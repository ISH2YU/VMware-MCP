"""Async facade over the blocking pyVmomi session."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, TypeVar

import anyio
import anyio.to_thread
from pyVmomi import vim, vmodl

from ..config import VSphereSettings
from ..errors import (
    ObjectNotFoundError,
    VMwareMCPError,
)
from .lookup import EntityKind, resolve_entity
from .query import (
    InventoryPathIndex,
    ObjectRecord,
    collect_properties,
    collect_properties_for,
)
from .session import VSphereSession

logger = logging.getLogger(__name__)

T = TypeVar("T")


def translate_fault(exc: BaseException) -> VMwareMCPError | None:
    """Map a pyVmomi fault onto one of our error types, if we recognise it."""
    if isinstance(exc, vim.fault.NoPermission):
        privilege = getattr(exc, "privilegeId", None)
        return VMwareMCPError(
            "The vSphere account is not permitted to perform this operation"
            + (f" (missing privilege {privilege})." if privilege else ".")
        )
    if isinstance(exc, vmodl.fault.ManagedObjectNotFound):
        return ObjectNotFoundError(
            "The object no longer exists in the vSphere inventory; it may have been removed."
        )
    if isinstance(exc, vim.fault.InvalidState):
        return VMwareMCPError(
            f"vSphere rejected the request because the object is in an invalid state: "
            f"{getattr(exc, 'msg', None) or exc}"
        )
    if isinstance(exc, vmodl.MethodFault):
        return VMwareMCPError(getattr(exc, "msg", None) or f"vSphere error: {type(exc).__name__}")
    return None


class VSphereClient:
    """Runs vSphere work on worker threads and caches the inventory tree.

    Every public method is a coroutine; the pyVmomi calls underneath run in a
    thread pool bounded by ``VMWARE_MAX_CONCURRENCY`` so a chatty client cannot
    open an unbounded number of connections to vCenter.
    """

    def __init__(self, settings: VSphereSettings, session: VSphereSession | None = None) -> None:
        self.settings = settings
        self._session = session or VSphereSession(settings)
        self._limiter = anyio.CapacityLimiter(settings.max_concurrency)
        self._index_lock = anyio.Lock()
        self._index: InventoryPathIndex | None = None
        self._index_fetched_at = 0.0

    # -- plumbing ---------------------------------------------------------- #

    async def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``func(service_instance, *args, **kwargs)`` off the event loop."""
        work = partial(self._session.call, func, *args, **kwargs)
        try:
            return await anyio.to_thread.run_sync(
                work, limiter=self._limiter, abandon_on_cancel=True
            )
        except Exception as exc:
            translated = translate_fault(exc)
            if translated is not None:
                raise translated from exc
            raise

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._session.close)

    # -- inventory --------------------------------------------------------- #

    async def path_index(self, *, refresh: bool = False) -> InventoryPathIndex:
        """The cached inventory tree used to render paths and parent names."""
        ttl = self.settings.cache_ttl
        async with self._index_lock:
            fresh = (
                self._index is not None
                and not refresh
                and (ttl > 0 and time.monotonic() - self._index_fetched_at < ttl)
            )
            if not fresh:
                self._index = await self.call(InventoryPathIndex.build)
                self._index_fetched_at = time.monotonic()
            assert self._index is not None
            return self._index

    async def collect(
        self,
        obj_type: type,
        path_set: Sequence[str],
        *,
        container_moid: str | None = None,
        container_type: type | None = None,
        recursive: bool = True,
    ) -> list[ObjectRecord]:
        """Property-collector fetch, optionally scoped to a container object."""

        def work(service_instance: vim.ServiceInstance) -> list[ObjectRecord]:
            container = None
            if container_moid and container_type is not None:
                container = container_type(container_moid, service_instance._stub)
            return collect_properties(
                service_instance, obj_type, path_set, container=container, recursive=recursive
            )

        return await self.call(work)

    async def resolve(
        self,
        kind: EntityKind,
        identifier: str,
        *,
        extra_properties: Sequence[str] = (),
        index: InventoryPathIndex | None = None,
    ) -> ObjectRecord:
        resolved_index = index if index is not None else await self.path_index()

        def work(service_instance: vim.ServiceInstance) -> ObjectRecord:
            return resolve_entity(
                service_instance,
                kind,
                identifier,
                resolved_index,
                extra_properties=extra_properties,
            )

        return await self.call(work)

    async def properties_for(
        self, obj_type: type, moid: str, path_set: Sequence[str]
    ) -> ObjectRecord:
        """Fetch ``path_set`` for a single known object."""

        def work(service_instance: vim.ServiceInstance) -> ObjectRecord:
            target = obj_type(moid, service_instance._stub)
            records = collect_properties_for(service_instance, obj_type, [target], path_set)
            if not records:
                raise ObjectNotFoundError(f"Object {moid} was not found in the inventory.")
            return records[0]

        return await self.call(work)

    # -- server metadata --------------------------------------------------- #

    async def about(self) -> dict[str, Any]:
        def work(service_instance: vim.ServiceInstance) -> dict[str, Any]:
            content = service_instance.RetrieveContent()
            about = content.about
            session = getattr(content.sessionManager, "currentSession", None)
            return {
                "about": about,
                "session_user": getattr(session, "userName", None),
                "session_locale": getattr(session, "locale", None),
                "server_clock": service_instance.CurrentTime(),
            }

        return await self.call(work)
