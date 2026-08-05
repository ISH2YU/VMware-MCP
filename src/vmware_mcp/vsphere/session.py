"""Connection handling for vCenter Server / ESXi.

pyVmomi is entirely synchronous, so everything in this module blocks. The async
side of the server never touches it directly; :mod:`vmware_mcp.vsphere.client`
pushes these calls onto worker threads.
"""

from __future__ import annotations

import logging
import ssl
import threading
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from ..config import VSphereSettings
from ..errors import ConnectionFailedError

logger = logging.getLogger(__name__)

# vCenter rejects a request with NotAuthenticated when the session has expired.
# The request never ran, so replaying it after reconnecting is safe.
_SESSION_FAULTS = (vim.fault.NotAuthenticated,)


def build_ssl_context(settings: VSphereSettings) -> ssl.SSLContext:
    """SSL context honouring ``VMWARE_VERIFY_SSL`` / ``VMWARE_CA_BUNDLE``."""
    if not settings.verify_ssl:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return ssl.create_default_context(cafile=settings.ca_bundle)


class VSphereSession:
    """Owns a single :class:`vim.ServiceInstance` and keeps it alive.

    The SOAP stub underneath pyVmomi maintains its own connection pool and is
    safe to use from several threads, so only session creation and teardown are
    serialised here.
    """

    def __init__(self, settings: VSphereSettings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._service_instance: vim.ServiceInstance | None = None

    @property
    def connected(self) -> bool:
        return self._service_instance is not None

    def service_instance(self) -> vim.ServiceInstance:
        """Return a live ``ServiceInstance``, connecting on first use."""
        instance = self._service_instance
        if instance is not None:
            return instance
        with self._lock:
            if self._service_instance is None:
                self._service_instance = self._connect()
            return self._service_instance

    def reconnect(self) -> vim.ServiceInstance:
        """Drop the current session and establish a fresh one."""
        with self._lock:
            self._disconnect_locked()
            self._service_instance = self._connect()
            return self._service_instance

    def close(self) -> None:
        with self._lock:
            self._disconnect_locked()

    def _connect(self) -> vim.ServiceInstance:
        settings = self._settings
        if not settings.verify_ssl:
            logger.warning(
                "TLS certificate verification is disabled for %s; "
                "this is unsafe outside of lab environments.",
                settings.endpoint,
            )
        logger.info("Connecting to vSphere endpoint %s as %s", settings.endpoint, settings.username)
        try:
            instance = SmartConnect(
                host=settings.host,
                port=settings.port,
                user=settings.username,
                pwd=settings.password,
                sslContext=build_ssl_context(settings),
                disableSslCertValidation=not settings.verify_ssl,
                httpConnectionTimeout=settings.connect_timeout,
            )
        except vim.fault.InvalidLogin as exc:
            raise ConnectionFailedError(
                f"vSphere rejected the credentials for {settings.username} at {settings.endpoint}."
            ) from exc
        except ssl.SSLError as exc:
            raise ConnectionFailedError(
                f"TLS handshake with {settings.endpoint} failed: {exc}. Point "
                "VMWARE_CA_BUNDLE at the vCenter CA certificate, or set "
                "VMWARE_VERIFY_SSL=false for a lab deployment."
            ) from exc
        except (vmodl.MethodFault, OSError) as exc:
            detail = getattr(exc, "msg", None) or str(exc)
            raise ConnectionFailedError(
                f"Could not connect to {settings.endpoint}: {detail}"
            ) from exc
        if instance is None:  # pragma: no cover - SmartConnect raises in practice
            raise ConnectionFailedError(f"Could not connect to {settings.endpoint}.")
        return instance

    def _disconnect_locked(self) -> None:
        instance, self._service_instance = self._service_instance, None
        if instance is None:
            return
        try:
            Disconnect(instance)
        except Exception:
            logger.debug("Ignoring error while disconnecting from vSphere", exc_info=True)

    def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run ``func(service_instance, *args)`` with one reconnect on session loss."""
        try:
            return func(self.service_instance(), *args, **kwargs)
        except _SESSION_FAULTS:
            logger.info("vSphere session expired; reconnecting to %s", self._settings.endpoint)
            return func(self.reconnect(), *args, **kwargs)
