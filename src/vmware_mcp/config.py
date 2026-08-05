"""Environment-driven configuration for local VMware Workstation / Fusion / Player."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .errors import ConfigurationError, PermissionDeniedError

ENV_PREFIX = "VMWARE_"

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})


class Product(str, Enum):
    """The ``vmrun -T`` host type."""

    WORKSTATION = "ws"
    FUSION = "fusion"
    PLAYER = "player"

    @classmethod
    def parse(cls, raw: str) -> Product:
        normalized = raw.strip().lower()
        aliases = {
            "ws": cls.WORKSTATION,
            "workstation": cls.WORKSTATION,
            "fusion": cls.FUSION,
            "vmware fusion": cls.FUSION,
            "player": cls.PLAYER,
            "vmware player": cls.PLAYER,
        }
        try:
            return aliases[normalized]
        except KeyError:
            raise ConfigurationError(
                f"Invalid product {raw!r}. Expected 'ws', 'fusion' or 'player'."
            ) from None

    @classmethod
    def detect(cls) -> Product:
        return cls.FUSION if sys.platform == "darwin" else cls.WORKSTATION


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


def default_vm_directories() -> tuple[Path, ...]:
    """Where VMware puts virtual machines by default, per platform."""
    home = Path.home()
    if sys.platform == "win32":
        candidates = [
            home / "Documents" / "Virtual Machines",
            home / "Virtual Machines",
        ]
    elif sys.platform == "darwin":
        candidates = [
            home / "Virtual Machines.localized",
            home / "Documents" / "Virtual Machines.localized",
            home / "Virtual Machines",
        ]
    else:
        candidates = [
            home / "vmware",
            home / "VMs",
            home / "Virtual Machines",
        ]
    return tuple(candidates)


def _split_paths(raw: str) -> tuple[Path, ...]:
    parts = [part.strip() for part in raw.split(os.pathsep)]
    return tuple(Path(part).expanduser() for part in parts if part)


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Everything the server needs to drive local VMs and behave itself."""

    vmrun_path: str | None = None
    product: Product = field(default_factory=Product.detect)
    vm_dirs: tuple[Path, ...] = ()
    guest_username: str | None = None
    guest_password: str | None = field(default=None, repr=False)
    guest_temp_dir: str | None = None
    vmx_password: str | None = field(default=None, repr=False)
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    command_timeout: int = 120
    guest_timeout: int = 300
    boot_timeout: int = 300
    max_output_bytes: int = 100_000
    max_results: int = 500
    default_page_size: int = 100
    cache_ttl: int = 5
    log_level: str = "INFO"

    @property
    def has_guest_credentials(self) -> bool:
        return bool(self.guest_username)

    def require(self, required: PermissionMode, operation: str) -> None:
        """Raise unless the configured mode permits ``operation``."""
        if self.permission_mode.allows(required):
            return
        raise PermissionDeniedError(
            f"{operation!r} requires permission mode {required.value!r}, but this server "
            f"is running in {self.permission_mode.value!r} mode. Set "
            f"{ENV_PREFIX}PERMISSION_MODE={required.value} to enable it."
        )

    def describe(self) -> dict[str, object]:
        """A redacted view of the settings, safe to return from a tool."""
        return {
            "product": self.product.value,
            "vmrun_path": self.vmrun_path,
            "vm_directories": [str(path) for path in self.vm_dirs],
            "guest_username": self.guest_username,
            "guest_credentials_configured": self.has_guest_credentials,
            "permission_mode": self.permission_mode.value,
            "command_timeout_seconds": self.command_timeout,
            "guest_timeout_seconds": self.guest_timeout,
            "boot_timeout_seconds": self.boot_timeout,
            "max_results": self.max_results,
            "default_page_size": self.default_page_size,
        }


class _EnvReader:
    def __init__(self, source: Mapping[str, str]) -> None:
        self._source = source

    def text(self, name: str, *aliases: str) -> str | None:
        for key in (ENV_PREFIX + name, *aliases):
            value = self._source.get(key)
            if value is not None and value.strip() != "":
                return value
        return None

    def raw(self, *names: str) -> str | None:
        """Like :meth:`text` but keeps a deliberately blank value (for passwords)."""
        for index, name in enumerate(names):
            key = ENV_PREFIX + name if index == 0 else name
            if key in self._source:
                return self._source[key]
        return None

    def number(self, name: str, default: int, minimum: int) -> int:
        value = self.text(name)
        if value is None:
            return default
        return _parse_int(value, name=ENV_PREFIX + name, minimum=minimum)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables."""
    reader = _EnvReader(os.environ if env is None else env)

    vmrun_path = reader.text("VMRUN_PATH", "VMWARE_VMRUN")
    if vmrun_path is not None and not Path(vmrun_path).expanduser().is_file():
        raise ConfigurationError(
            f"{ENV_PREFIX}VMRUN_PATH points at something that is not a file: {vmrun_path}"
        )

    product_raw = reader.text("PRODUCT", "VMWARE_HOST_TYPE")
    dirs_raw = reader.text("VM_DIRS", "VMWARE_VM_DIR")
    permission_raw = reader.text("PERMISSION_MODE")
    max_results = reader.number("MAX_RESULTS", 500, 1)

    return Settings(
        vmrun_path=str(Path(vmrun_path).expanduser()) if vmrun_path else None,
        product=Product.parse(product_raw) if product_raw else Product.detect(),
        vm_dirs=_split_paths(dirs_raw) if dirs_raw else default_vm_directories(),
        guest_username=reader.text("GUEST_USERNAME", "VMWARE_GUEST_USER"),
        guest_password=reader.raw("GUEST_PASSWORD"),
        guest_temp_dir=reader.text("GUEST_TEMP_DIR"),
        vmx_password=reader.raw("VMX_PASSWORD"),
        permission_mode=(
            PermissionMode.READ_ONLY
            if permission_raw is None
            else PermissionMode.parse(permission_raw)
        ),
        command_timeout=reader.number("COMMAND_TIMEOUT", 120, 1),
        guest_timeout=reader.number("GUEST_TIMEOUT", 300, 1),
        boot_timeout=reader.number("BOOT_TIMEOUT", 300, 1),
        max_output_bytes=reader.number("MAX_OUTPUT_BYTES", 100_000, 1024),
        max_results=max_results,
        default_page_size=min(reader.number("DEFAULT_PAGE_SIZE", 100, 1), max_results),
        cache_ttl=reader.number("CACHE_TTL", 5, 0),
        log_level=(reader.text("LOG_LEVEL") or "INFO").upper(),
    )


__all__: Sequence[str] = (
    "ENV_PREFIX",
    "PermissionMode",
    "Product",
    "Settings",
    "default_vm_directories",
    "load_settings",
)
