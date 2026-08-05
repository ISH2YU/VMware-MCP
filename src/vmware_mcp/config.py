"""Environment-driven configuration for the VMware MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from .errors import ConfigurationError, PermissionDeniedError

ENV_PREFIX = "VMWARE_"

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})


class PermissionMode(str, Enum):
    """How much damage the server is allowed to do.

    Modes are cumulative: ``destructive`` implies ``write`` implies ``read_only``.
    """

    READ_ONLY = "read-only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @classmethod
    def parse(cls, raw: str) -> PermissionMode:
        normalized = raw.strip().lower().replace("_", "-")
        aliases = {
            "read-only": cls.READ_ONLY,
            "readonly": cls.READ_ONLY,
            "ro": cls.READ_ONLY,
            "write": cls.WRITE,
            "read-write": cls.WRITE,
            "readwrite": cls.WRITE,
            "rw": cls.WRITE,
            "destructive": cls.DESTRUCTIVE,
            "full": cls.DESTRUCTIVE,
            "admin": cls.DESTRUCTIVE,
        }
        try:
            return aliases[normalized]
        except KeyError:
            raise ConfigurationError(
                f"Invalid permission mode {raw!r}. Expected one of: read-only, write, destructive."
            ) from None

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]

    def allows(self, required: PermissionMode) -> bool:
        return self.rank >= required.rank


_MODE_RANK: dict[PermissionMode, int] = {
    PermissionMode.READ_ONLY: 0,
    PermissionMode.WRITE: 1,
    PermissionMode.DESTRUCTIVE: 2,
}


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean value, got {value!r}.")


def _parse_int(value: str, *, name: str, minimum: int | None = None) -> int:
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer, got {value!r}.") from None
    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {parsed}.")
    return parsed


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to talk to vCenter/ESXi and behave itself."""

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 443
    verify_ssl: bool = True
    ca_bundle: str | None = None
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    connect_timeout: int = 30
    task_timeout: int = 600
    max_results: int = 500
    default_page_size: int = 100
    cache_ttl: int = 60
    max_concurrency: int = 8
    log_level: str = "INFO"

    def require(self, required: PermissionMode, operation: str) -> None:
        """Raise unless the configured mode permits ``operation``."""
        if self.permission_mode.allows(required):
            return
        raise PermissionDeniedError(
            f"{operation!r} requires permission mode {required.value!r}, but this server "
            f"is running in {self.permission_mode.value!r} mode. Set "
            f"{ENV_PREFIX}PERMISSION_MODE={required.value} to enable it."
        )

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def describe(self) -> dict[str, object]:
        """A redacted view of the settings, safe to return from a tool."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "verify_ssl": self.verify_ssl,
            "ca_bundle": self.ca_bundle,
            "permission_mode": self.permission_mode.value,
            "connect_timeout_seconds": self.connect_timeout,
            "task_timeout_seconds": self.task_timeout,
            "max_results": self.max_results,
            "default_page_size": self.default_page_size,
            "cache_ttl_seconds": self.cache_ttl,
            "max_concurrency": self.max_concurrency,
        }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables.

    Raises :class:`ConfigurationError` when required values are missing so the
    process fails at startup rather than on the first tool call.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    def get(name: str, *aliases: str) -> str | None:
        for key in (ENV_PREFIX + name, *aliases):
            value = source.get(key)
            if value is not None and value.strip() != "":
                return value
        return None

    host = get("HOST", "VSPHERE_HOST")
    if not host:
        raise ConfigurationError(
            f"{ENV_PREFIX}HOST is required (hostname or IP of vCenter Server or an ESXi host)."
        )
    username = get("USERNAME", "VMWARE_USER", "VSPHERE_USER")
    if not username:
        raise ConfigurationError(f"{ENV_PREFIX}USERNAME is required.")
    password = get("PASSWORD", "VSPHERE_PASSWORD")
    if password is None:
        raise ConfigurationError(f"{ENV_PREFIX}PASSWORD is required.")

    verify_raw = get("VERIFY_SSL")
    verify_ssl = (
        True if verify_raw is None else _parse_bool(verify_raw, name=f"{ENV_PREFIX}VERIFY_SSL")
    )
    insecure_raw = get("INSECURE")
    if insecure_raw is not None and _parse_bool(insecure_raw, name=f"{ENV_PREFIX}INSECURE"):
        verify_ssl = False

    ca_bundle = get("CA_BUNDLE")
    if ca_bundle is not None and not os.path.isfile(ca_bundle):
        raise ConfigurationError(f"{ENV_PREFIX}CA_BUNDLE points at a missing file: {ca_bundle}")

    permission_raw = get("PERMISSION_MODE")
    permission_mode = (
        PermissionMode.READ_ONLY if permission_raw is None else PermissionMode.parse(permission_raw)
    )

    def int_setting(name: str, default: int, minimum: int) -> int:
        raw = get(name)
        if raw is None:
            return default
        return _parse_int(raw, name=ENV_PREFIX + name, minimum=minimum)

    max_results = int_setting("MAX_RESULTS", 500, 1)
    default_page_size = min(int_setting("DEFAULT_PAGE_SIZE", 100, 1), max_results)

    return Settings(
        host=host,
        username=username,
        password=password,
        port=int_setting("PORT", 443, 1),
        verify_ssl=verify_ssl,
        ca_bundle=ca_bundle,
        permission_mode=permission_mode,
        connect_timeout=int_setting("CONNECT_TIMEOUT", 30, 1),
        task_timeout=int_setting("TASK_TIMEOUT", 600, 1),
        max_results=max_results,
        default_page_size=default_page_size,
        cache_ttl=int_setting("CACHE_TTL", 60, 0),
        max_concurrency=int_setting("MAX_CONCURRENCY", 8, 1),
        log_level=(get("LOG_LEVEL") or "INFO").upper(),
    )
