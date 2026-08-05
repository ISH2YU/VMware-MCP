"""Environment-driven configuration.

Two backends are supported. ``workstation`` drives VMware Workstation, Fusion or
Player on the machine the server runs on, through the ``vmrun`` command line
tool. ``vsphere`` talks to a vCenter Server or ESXi host over the API. The local
backend is the default; vSphere is selected by pointing ``VMWARE_HOST`` at a
server.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar

from .errors import ConfigurationError, PermissionDeniedError

ENV_PREFIX = "VMWARE_"

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})


class Backend(str, Enum):
    """Which VMware product the server drives."""

    WORKSTATION = "workstation"
    VSPHERE = "vsphere"

    @classmethod
    def parse(cls, raw: str) -> Backend:
        normalized = raw.strip().lower()
        aliases = {
            "workstation": cls.WORKSTATION,
            "ws": cls.WORKSTATION,
            "local": cls.WORKSTATION,
            "fusion": cls.WORKSTATION,
            "player": cls.WORKSTATION,
            "desktop": cls.WORKSTATION,
            "vsphere": cls.VSPHERE,
            "vcenter": cls.VSPHERE,
            "esxi": cls.VSPHERE,
        }
        try:
            return aliases[normalized]
        except KeyError:
            raise ConfigurationError(
                f"Invalid backend {raw!r}. Expected 'workstation' (local VMware "
                f"Workstation/Fusion/Player) or 'vsphere' (vCenter Server or ESXi)."
            ) from None


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


@dataclass(frozen=True, kw_only=True)
class BaseSettings:
    """Settings shared by both backends."""

    backend: ClassVar[Backend]

    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    max_results: int = 500
    default_page_size: int = 100
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

    def describe(self) -> dict[str, object]:
        """A redacted view of the settings, safe to return from a tool."""
        return {
            "backend": self.backend.value,
            "permission_mode": self.permission_mode.value,
            "max_results": self.max_results,
            "default_page_size": self.default_page_size,
        }


@dataclass(frozen=True, kw_only=True)
class WorkstationSettings(BaseSettings):
    """Settings for driving VMware Workstation, Fusion or Player locally."""

    backend: ClassVar[Backend] = Backend.WORKSTATION

    vmrun_path: str | None = None
    product: Product = field(default_factory=Product.detect)
    vm_dirs: tuple[Path, ...] = ()
    guest_username: str | None = None
    guest_password: str | None = field(default=None, repr=False)
    guest_temp_dir: str | None = None
    vmx_password: str | None = field(default=None, repr=False)
    command_timeout: int = 120
    guest_timeout: int = 300
    boot_timeout: int = 300
    max_output_bytes: int = 100_000
    cache_ttl: int = 5

    @property
    def has_guest_credentials(self) -> bool:
        return bool(self.guest_username)

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "product": self.product.value,
            "vmrun_path": self.vmrun_path,
            "vm_directories": [str(path) for path in self.vm_dirs],
            "guest_username": self.guest_username,
            "guest_credentials_configured": self.has_guest_credentials,
            "command_timeout_seconds": self.command_timeout,
            "guest_timeout_seconds": self.guest_timeout,
            "boot_timeout_seconds": self.boot_timeout,
        }


@dataclass(frozen=True, kw_only=True)
class VSphereSettings(BaseSettings):
    """Settings for talking to vCenter Server or a standalone ESXi host."""

    backend: ClassVar[Backend] = Backend.VSPHERE

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 443
    verify_ssl: bool = True
    ca_bundle: str | None = None
    connect_timeout: int = 30
    task_timeout: int = 600
    cache_ttl: int = 60
    max_concurrency: int = 8

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "verify_ssl": self.verify_ssl,
            "ca_bundle": self.ca_bundle,
            "connect_timeout_seconds": self.connect_timeout,
            "task_timeout_seconds": self.task_timeout,
            "cache_ttl_seconds": self.cache_ttl,
            "max_concurrency": self.max_concurrency,
        }


Settings = BaseSettings


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


class _EnvReader:
    """Reads ``VMWARE_*`` variables, with support for legacy aliases."""

    def __init__(self, source: Mapping[str, str]) -> None:
        self._source = source

    def text(self, name: str, *aliases: str) -> str | None:
        for key in (ENV_PREFIX + name, *aliases):
            value = self._source.get(key)
            if value is not None and value.strip() != "":
                return value
        return None

    def raw(self, *names: str) -> str | None:
        """Like :meth:`text` but keeps a deliberately blank value.

        Passwords are the reason this exists: an empty one is a real choice.
        """
        for index, name in enumerate(names):
            key = ENV_PREFIX + name if index == 0 else name
            if key in self._source:
                return self._source[key]
        return None

    def flag(self, name: str, default: bool) -> bool:
        value = self.text(name)
        if value is None:
            return default
        return _parse_bool(value, name=ENV_PREFIX + name)

    def number(self, name: str, default: int, minimum: int) -> int:
        value = self.text(name)
        if value is None:
            return default
        return _parse_int(value, name=ENV_PREFIX + name, minimum=minimum)


def detect_backend(env: Mapping[str, str]) -> Backend:
    """Pick a backend from the environment.

    An explicit ``VMWARE_BACKEND`` wins. Otherwise the presence of a server
    hostname means vSphere, and everything else means the local hypervisor.
    """
    reader = _EnvReader(env)
    explicit = reader.text("BACKEND", "VMWARE_MCP_BACKEND")
    if explicit:
        return Backend.parse(explicit)
    if reader.text("HOST", "VSPHERE_HOST"):
        return Backend.VSPHERE
    return Backend.WORKSTATION


def _common_kwargs(reader: _EnvReader) -> dict[str, object]:
    permission_raw = reader.text("PERMISSION_MODE")
    max_results = reader.number("MAX_RESULTS", 500, 1)
    return {
        "permission_mode": (
            PermissionMode.READ_ONLY
            if permission_raw is None
            else PermissionMode.parse(permission_raw)
        ),
        "max_results": max_results,
        "default_page_size": min(reader.number("DEFAULT_PAGE_SIZE", 100, 1), max_results),
        "log_level": (reader.text("LOG_LEVEL") or "INFO").upper(),
    }


def load_workstation_settings(env: Mapping[str, str] | None = None) -> WorkstationSettings:
    reader = _EnvReader(os.environ if env is None else env)

    vmrun_path = reader.text("VMRUN_PATH", "VMWARE_VMRUN")
    if vmrun_path is not None and not Path(vmrun_path).expanduser().is_file():
        raise ConfigurationError(
            f"{ENV_PREFIX}VMRUN_PATH points at something that is not a file: {vmrun_path}"
        )

    product_raw = reader.text("PRODUCT", "VMWARE_HOST_TYPE")
    dirs_raw = reader.text("VM_DIRS", "VMWARE_VM_DIR")

    return WorkstationSettings(
        vmrun_path=str(Path(vmrun_path).expanduser()) if vmrun_path else None,
        product=Product.parse(product_raw) if product_raw else Product.detect(),
        vm_dirs=_split_paths(dirs_raw) if dirs_raw else default_vm_directories(),
        guest_username=reader.text("GUEST_USERNAME", "VMWARE_GUEST_USER"),
        guest_password=reader.raw("GUEST_PASSWORD"),
        guest_temp_dir=reader.text("GUEST_TEMP_DIR"),
        vmx_password=reader.raw("VMX_PASSWORD"),
        command_timeout=reader.number("COMMAND_TIMEOUT", 120, 1),
        guest_timeout=reader.number("GUEST_TIMEOUT", 300, 1),
        boot_timeout=reader.number("BOOT_TIMEOUT", 300, 1),
        max_output_bytes=reader.number("MAX_OUTPUT_BYTES", 100_000, 1024),
        cache_ttl=reader.number("CACHE_TTL", 5, 0),
        **_common_kwargs(reader),  # type: ignore[arg-type]
    )


def load_vsphere_settings(env: Mapping[str, str] | None = None) -> VSphereSettings:
    reader = _EnvReader(os.environ if env is None else env)

    host = reader.text("HOST", "VSPHERE_HOST")
    if not host:
        raise ConfigurationError(
            f"{ENV_PREFIX}HOST is required (hostname or IP of vCenter Server or an ESXi host)."
        )
    username = reader.text("USERNAME", "VMWARE_USER", "VSPHERE_USER")
    if not username:
        raise ConfigurationError(f"{ENV_PREFIX}USERNAME is required.")
    password = reader.raw("PASSWORD", "VSPHERE_PASSWORD")
    if password is None:
        raise ConfigurationError(f"{ENV_PREFIX}PASSWORD is required.")

    verify_ssl = reader.flag("VERIFY_SSL", True)
    if reader.flag("INSECURE", False):
        verify_ssl = False

    ca_bundle = reader.text("CA_BUNDLE")
    if ca_bundle is not None and not os.path.isfile(ca_bundle):
        raise ConfigurationError(f"{ENV_PREFIX}CA_BUNDLE points at a missing file: {ca_bundle}")

    return VSphereSettings(
        host=host,
        username=username,
        password=password,
        port=reader.number("PORT", 443, 1),
        verify_ssl=verify_ssl,
        ca_bundle=ca_bundle,
        connect_timeout=reader.number("CONNECT_TIMEOUT", 30, 1),
        task_timeout=reader.number("TASK_TIMEOUT", 600, 1),
        cache_ttl=reader.number("CACHE_TTL", 60, 0),
        max_concurrency=reader.number("MAX_CONCURRENCY", 8, 1),
        **_common_kwargs(reader),  # type: ignore[arg-type]
    )


def load_settings(env: Mapping[str, str] | None = None) -> BaseSettings:
    """Build settings for whichever backend the environment selects."""
    source: Mapping[str, str] = os.environ if env is None else env
    if detect_backend(source) is Backend.VSPHERE:
        return load_vsphere_settings(source)
    return load_workstation_settings(source)


__all__: Sequence[str] = (
    "ENV_PREFIX",
    "Backend",
    "BaseSettings",
    "PermissionMode",
    "Product",
    "Settings",
    "VSphereSettings",
    "WorkstationSettings",
    "default_vm_directories",
    "detect_backend",
    "load_settings",
    "load_vsphere_settings",
    "load_workstation_settings",
)
