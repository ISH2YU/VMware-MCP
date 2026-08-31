"""Finding local virtual machines and resolving user-supplied identifiers."""

from __future__ import annotations

import fnmatch
import logging
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from ..errors import AmbiguousObjectError, ObjectNotFoundError
from .paths import normalize_path, path_is_within_any, require_within_vm_dirs
from .vmx import VmxFile, guest_os_family, load_vmx

logger = logging.getLogger(__name__)

#: Directory names that never contain a VM worth managing.
_SKIP_DIRECTORIES = frozenset(
    {"__pycache__", "node_modules", "$recycle.bin", "system volume information"}
)


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


#: ``VmInventory.list`` shadows the builtin inside the class body, so annotations
#: there use this alias instead.
VmList = list[DiscoveredVm]


def discover_vmx_files(
    directories: Sequence[Path],
    *,
    max_depth: int = 4,
    max_files: int = 5000,
) -> list[Path]:
    """Walk ``directories`` looking for ``.vmx`` files.

    Skips clone scratch dirs and anything under a ``.lck`` lock directory. The
    walk is breadth-first, depth-capped and remembers which real directories it
    has already entered, so a symlink pointing at an ancestor cannot spin
    forever and a mis-pointed ``VMWARE_VM_DIRS`` at ``/`` cannot hang the
    process.

    Every hit is checked against the resolved roots before it is returned: a
    symlink sitting in the library but pointing at a ``.vmx`` somewhere else is
    not a VM this server is allowed to touch.
    """
    found: list[Path] = []
    seen_files: set[str] = set()
    visited_dirs: set[str] = set()

    roots: list[Path] = []
    for directory in directories:
        try:
            resolved_root = directory.expanduser().resolve()
        except OSError:
            continue
        if resolved_root.is_dir():
            roots.append(resolved_root)
        else:
            logger.debug("VM directory does not exist, skipping: %s", resolved_root)

    for root in roots:
        queue: deque[tuple[Path, int]] = deque([(root, max_depth)])
        while queue:
            current, depth = queue.popleft()
            key = normalize_path(current)
            if key in visited_dirs:
                continue
            visited_dirs.add(key)
            try:
                entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except (PermissionError, OSError) as exc:
                logger.debug("Cannot read %s: %s", current, exc)
                continue
            for entry in entries:
                name = entry.name
                lowered = name.lower()
                if name.startswith(".") or lowered.endswith(".lck"):
                    continue
                if lowered in _SKIP_DIRECTORIES:
                    continue
                try:
                    is_dir = entry.is_dir()
                    is_file = entry.is_file()
                except OSError:
                    continue
                if is_file and lowered.endswith(".vmx"):
                    try:
                        resolved = entry.resolve()
                    except OSError:
                        continue
                    if not path_is_within_any(resolved, roots):
                        logger.warning(
                            "Ignoring %s: it points outside the configured VM directories.",
                            entry,
                        )
                        continue
                    marker = normalize_path(resolved)
                    if marker in seen_files:
                        continue
                    seen_files.add(marker)
                    found.append(resolved)
                    if len(found) >= max_files:
                        logger.warning(
                            "Stopped scanning after %d .vmx files; narrow VMWARE_VM_DIRS.",
                            max_files,
                        )
                        return sorted(found, key=lambda item: item.name.lower())
                elif is_dir and depth > 0:
                    queue.append((entry, depth - 1))

    return sorted(found, key=lambda item: item.name.lower())


class VmInventory:
    """Cached catalogue of local VMs, keyed for flexible lookup.

    Scanning touches the filesystem, so refreshes happen in a worker thread and
    are serialised by a lock: several concurrent tool calls share one scan
    instead of each launching their own.
    """

    def __init__(self, directories: Sequence[Path], *, ttl: float = 5.0) -> None:
        self._directories = tuple(directories)
        self._ttl = ttl
        self._vms: VmList = []
        self._fetched_at: float | None = None
        self._lock = anyio.Lock()

    @property
    def directories(self) -> tuple[Path, ...]:
        return self._directories

    def _is_fresh(self) -> bool:
        return self._fetched_at is not None and (time.monotonic() - self._fetched_at) < self._ttl

    def _scan(self) -> VmList:
        discovered: VmList = []
        for path in discover_vmx_files(self._directories):
            try:
                discovered.append(DiscoveredVm.from_vmx(load_vmx(path)))
            except Exception:
                logger.warning("Skipping unreadable .vmx at %s", path, exc_info=True)
        return discovered

    def refresh(self, *, force: bool = False) -> VmList:
        """Synchronous refresh, used from non-async call sites and tests."""
        if not force and self._is_fresh():
            return list(self._vms)
        self._vms = self._scan()
        self._fetched_at = time.monotonic()
        return list(self._vms)

    async def refresh_async(self, *, force: bool = False) -> VmList:
        """Refresh off the event loop, collapsing concurrent callers into one scan."""
        if not force and self._is_fresh():
            return list(self._vms)
        async with self._lock:
            if not force and self._is_fresh():
                return list(self._vms)
            self._vms = await anyio.to_thread.run_sync(self._scan)
            self._fetched_at = time.monotonic()
            return list(self._vms)

    def invalidate(self) -> None:
        """Drop the cache so the next lookup rescans."""
        self._fetched_at = None

    def list(self) -> VmList:
        return self.refresh()

    async def list_async(self) -> VmList:
        return await self.refresh_async()

    def resolve(self, identifier: str) -> DiscoveredVm:
        """Find exactly one VM by name, path, stem or UUID.

        A ``.vmx`` path is accepted only when it sits under one of the configured
        VM directories. That is the sandbox: tools cannot power, clone or delete
        machines the operator did not expose via ``VMWARE_VM_DIRS``.
        """
        return self._match(identifier, self.refresh())

    async def resolve_async(self, identifier: str) -> DiscoveredVm:
        return self._match(identifier, await self.refresh_async())

    def _match(self, identifier: str, vms: VmList) -> DiscoveredVm:
        needle = identifier.strip()
        if not needle:
            raise ObjectNotFoundError("VM identifier must not be empty.")

        as_path = Path(needle).expanduser()
        if as_path.suffix.lower() == ".vmx":
            resolved = require_within_vm_dirs(as_path, self._directories, what="VM")
            target = normalize_path(resolved)
            for vm in vms:
                if normalize_path(vm.path) == target:
                    return vm
            if resolved.is_file():
                return DiscoveredVm.from_vmx(load_vmx(resolved))
            raise ObjectNotFoundError(f"No .vmx file at {resolved}.")

        lowered = needle.lower()
        for tier in (
            [vm for vm in vms if vm.uuid and vm.uuid.lower() == lowered],
            [vm for vm in vms if vm.name == needle],
            [vm for vm in vms if vm.name.lower() == lowered],
            [vm for vm in vms if vm.path.stem.lower() == lowered],
            [vm for vm in vms if vm.path.parent.name.lower() == lowered],
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
    """Case-insensitive match; ``*``/``?`` switch from substring to glob matching."""
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


__all__ = [
    "DiscoveredVm",
    "VmInventory",
    "discover_vmx_files",
    "name_matches",
]
