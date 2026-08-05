"""Finding local virtual machines and resolving user-supplied identifiers."""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AmbiguousObjectError, ObjectNotFoundError
from .vmx import VmxFile, guest_os_family, load_vmx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredVm:
    """One local virtual machine, identified by its ``.vmx`` path."""

    path: Path
    name: str
    guest_os: str | None
    guest_os_family: str | None
    memory_mb: int | None
    cpu_count: int | None
    uuid: str | None

    @classmethod
    def from_vmx(cls, vmx: VmxFile) -> DiscoveredVm:
        return cls(
            path=vmx.path.resolve(),
            name=vmx.get("displayname") or vmx.path.stem,
            guest_os=vmx.get("guestos"),
            guest_os_family=guest_os_family(vmx.get("guestos")),
            memory_mb=_as_int(vmx.get("memsize")),
            cpu_count=_as_int(vmx.get("numvcpus")) or 1,
            uuid=vmx.get("uuid.bios"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "guest_os": self.guest_os,
            "guest_os_family": self.guest_os_family,
            "cpu_count": self.cpu_count,
            "memory_mb": self.memory_mb,
            "memory_gib": round(self.memory_mb / 1024, 2) if self.memory_mb else None,
            "uuid": self.uuid,
        }


def discover_vmx_files(
    directories: list[Path] | tuple[Path, ...], *, max_depth: int = 4
) -> list[Path]:
    """Walk ``directories`` looking for ``.vmx`` files.

    Skips clone scratch dirs (``*-Snapshot*``) and anything under a ``.lck``
    lock directory. Depth is capped so a mis-pointed ``VMWARE_VM_DIRS`` at ``/``
    does not hang the process.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        root = directory.expanduser().resolve()
        if not root.is_dir():
            logger.debug("VM directory does not exist, skipping: %s", root)
            continue
        for path in _walk_vmx(root, max_depth=max_depth):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    return sorted(found, key=lambda item: item.name.lower())


def _walk_vmx(root: Path, *, max_depth: int) -> list[Path]:
    results: list[Path] = []
    try:
        for entry in root.iterdir():
            name = entry.name
            if name.startswith(".") or name.endswith(".lck"):
                continue
            if entry.is_file() and name.lower().endswith(".vmx"):
                results.append(entry)
            elif entry.is_dir() and max_depth > 0:
                results.extend(_walk_vmx(entry, max_depth=max_depth - 1))
    except PermissionError:
        logger.debug("Permission denied reading %s", root)
    return results


class VmInventory:
    """Cached catalogue of local VMs, keyed for flexible lookup."""

    def __init__(self, directories: list[Path] | tuple[Path, ...], *, ttl: float = 5.0) -> None:
        self._directories = tuple(directories)
        self._ttl = ttl
        self._vms: list[DiscoveredVm] = []
        self._fetched_at = 0.0

    def refresh(self, *, force: bool = False) -> list[DiscoveredVm]:
        if not force and self._vms and (time.monotonic() - self._fetched_at) < self._ttl:
            return list(self._vms)
        discovered: list[DiscoveredVm] = []
        for path in discover_vmx_files(self._directories):
            try:
                discovered.append(DiscoveredVm.from_vmx(load_vmx(path)))
            except Exception:
                logger.warning("Skipping unreadable .vmx at %s", path, exc_info=True)
        self._vms = discovered
        self._fetched_at = time.monotonic()
        return list(self._vms)

    def list(self) -> list[DiscoveredVm]:
        return self.refresh()

    def resolve(self, identifier: str) -> DiscoveredVm:
        """Find exactly one VM by name, path, stem or UUID."""
        needle = identifier.strip()
        if not needle:
            raise ObjectNotFoundError("VM identifier must not be empty.")
        vms = self.refresh()

        as_path = Path(needle).expanduser()
        if as_path.suffix.lower() == ".vmx" or as_path.exists():
            resolved = as_path.resolve()
            for vm in vms:
                if vm.path == resolved:
                    return vm
            if resolved.is_file() and resolved.suffix.lower() == ".vmx":
                return DiscoveredVm.from_vmx(load_vmx(resolved))

        lowered = needle.lower()
        for tier in (
            [vm for vm in vms if str(vm.path).lower() == lowered],
            [vm for vm in vms if vm.uuid and vm.uuid.lower() == lowered],
            [vm for vm in vms if vm.name == needle],
            [vm for vm in vms if vm.path.stem.lower() == lowered],
            [vm for vm in vms if vm.name.lower() == lowered],
        ):
            if len(tier) == 1:
                return tier[0]
            if len(tier) > 1:
                options = ", ".join(f"{vm.name} ({vm.path})" for vm in tier)
                raise AmbiguousObjectError(
                    f"{len(tier)} VMs match {identifier!r}: {options}. "
                    f"Pass the full .vmx path to disambiguate."
                )

        available = ", ".join(vm.name for vm in vms) or "none found"
        raise ObjectNotFoundError(
            f"No VM matches {identifier!r}. Known VMs: {available}. "
            f"Accepted identifiers are the display name, the .vmx path, the directory "
            f"name, or the BIOS UUID."
        )


def name_matches(value: str | None, pattern: str | None) -> bool:
    if not pattern:
        return True
    if value is None:
        return False
    lowered_value = value.lower()
    lowered_pattern = pattern.lower()
    if any(char in lowered_pattern for char in "*?["):
        return fnmatch.fnmatch(lowered_value, lowered_pattern)
    return lowered_pattern in lowered_value


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None
