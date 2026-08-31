"""Parse and edit VMware ``.vmx`` configuration files.

A ``.vmx`` is a key=value text file (optionally UTF-8 with a BOM). Guest OS,
CPU, memory and display name all live here; ``vmrun`` never rewrites them, so
reconfiguration means editing this file while the VM is powered off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InvalidArgumentError, ObjectNotFoundError

# Encoding declaration VMware writes at the top of every .vmx it creates.
_ENCODING_LINE = re.compile(r'^\.encoding\s*=\s*"([^"]*)"', re.IGNORECASE)
_ENTRY = re.compile(r'^([^=]+?)\s*=\s*"(.*)"\s*$')
_UNQUOTED = re.compile(r"^([^=]+?)\s*=\s*(.*?)\s*$")

BYTES_PER_MIB = 1024 * 1024


@dataclass
class VmxFile:
    """A loaded ``.vmx`` with order-preserving key access."""

    path: Path
    encoding: str = "UTF-8"
    entries: dict[str, str] = field(default_factory=dict)
    # Keys in the order they appeared; used so a rewrite does not scramble the file.
    order: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.entries.get(key.lower(), default)

    def set(self, key: str, value: str | int | bool) -> None:
        normalized = key.lower()
        rendered = _render_value(value)
        if normalized not in self.entries:
            self.order.append(normalized)
        self.entries[normalized] = rendered

    def delete(self, key: str) -> None:
        normalized = key.lower()
        self.entries.pop(normalized, None)
        self.order = [item for item in self.order if item != normalized]

    def as_text(self) -> str:
        lines = [f'.encoding = "{self.encoding}"']
        for key in self.order:
            if key == ".encoding":
                continue
            if key in self.entries:
                lines.append(f'{key} = "{_escape(self.entries[key])}"')
        return "\n".join(lines) + "\n"

    def write(self) -> None:
        """Atomically replace the ``.vmx`` so a crash cannot leave a half-written file."""
        data = self.as_text()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(data, encoding=self.encoding)
            tmp.replace(self.path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def summary(self) -> dict[str, Any]:
        """The fields an operator usually cares about."""
        memory_mb = _as_int(self.get("memsize"))
        num_vcpus = _as_int(self.get("numvcpus")) or 1
        cores_per_socket = _as_int(self.get("cpuid.corespersocket")) or num_vcpus
        guest_os = self.get("guestos")
        return {
            "name": self.get("displayname") or self.path.stem,
            "path": str(self.path.resolve()),
            "guest_os": guest_os,
            "guest_os_family": guest_os_family(guest_os),
            "firmware": "efi" if (self.get("firmware") or "").lower() == "efi" else "bios",
            "cpu_count": num_vcpus,
            "cores_per_socket": cores_per_socket,
            "memory_mb": memory_mb,
            "memory_gib": round(memory_mb / 1024, 2) if memory_mb else None,
            "annotation": self.get("annotation"),
            "uuid": self.get("uuid.bios"),
            "nvram": self.get("nvram"),
            "ethernet": _ethernet_adapters(self),
            "disks": _virtual_disks(self),
        }


def load_vmx(path: Path | str) -> VmxFile:
    """Read a ``.vmx`` file into a :class:`VmxFile`."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise ObjectNotFoundError(f"No .vmx file at {target}.")

    raw = target.read_bytes()
    encoding = "utf-8"
    text = raw.decode("utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text else ""
    match = _ENCODING_LINE.match(first)
    if match:
        encoding = match.group(1) or "utf-8"
        if encoding.lower() not in {"utf-8", "utf8"}:
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                encoding = "utf-8"

    entries: dict[str, str] = {}
    order: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _ENTRY.match(stripped) or _UNQUOTED.match(stripped)
        if not parsed:
            continue
        key = parsed.group(1).strip().lower()
        value = _unescape(parsed.group(2).strip().strip('"'))
        if key not in entries:
            order.append(key)
        entries[key] = value
        if key == ".encoding":
            encoding = value or encoding

    return VmxFile(
        path=target, encoding=encoding, entries=entries, order=order, raw_lines=text.splitlines()
    )


def guest_os_family(guest_os: str | None) -> str | None:
    """Map a VMware ``guestOS`` identifier onto a coarse family."""
    if not guest_os:
        return None
    lowered = guest_os.lower()
    if lowered.startswith("win") or "windows" in lowered:
        return "windows"
    if any(
        token in lowered
        for token in (
            "ubuntu",
            "debian",
            "rhel",
            "centos",
            "fedora",
            "sles",
            "linux",
            "otherlinux",
        )
    ):
        return "linux"
    if "darwin" in lowered or "macos" in lowered:
        return "macos"
    if "freebsd" in lowered or "openbsd" in lowered or "netbsd" in lowered:
        return "bsd"
    if "solaris" in lowered:
        return "solaris"
    return "other"


def apply_config_changes(
    vmx: VmxFile,
    *,
    name: str | None = None,
    cpu_count: int | None = None,
    cores_per_socket: int | None = None,
    memory_mb: int | None = None,
    annotation: str | None = None,
) -> dict[str, Any]:
    """Apply configuration changes and return a before/after summary.

    Caller must ensure the VM is powered off; writing a live ``.vmx`` is unsafe.
    """
    if cpu_count is not None and cpu_count < 1:
        raise InvalidArgumentError("cpu_count must be at least 1.")
    if cpu_count is not None and cpu_count > 256:
        raise InvalidArgumentError("cpu_count must be at most 256.")
    if memory_mb is not None and memory_mb < 4:
        raise InvalidArgumentError("memory_mb must be at least 4.")
    if memory_mb is not None and memory_mb > 1_048_576:
        raise InvalidArgumentError("memory_mb must be at most 1048576 (1 TiB).")
    if cores_per_socket is not None:
        if cores_per_socket < 1:
            raise InvalidArgumentError("cores_per_socket must be at least 1.")
        effective_cpus = cpu_count if cpu_count is not None else (_as_int(vmx.get("numvcpus")) or 1)
        if effective_cpus % cores_per_socket != 0:
            raise InvalidArgumentError(
                f"cpu_count ({effective_cpus}) must be a multiple of cores_per_socket "
                f"({cores_per_socket})."
            )

    before = {
        "name": vmx.get("displayname"),
        "cpu_count": _as_int(vmx.get("numvcpus")),
        "cores_per_socket": _as_int(vmx.get("cpuid.corespersocket")),
        "memory_mb": _as_int(vmx.get("memsize")),
        "annotation": vmx.get("annotation"),
    }
    if name is not None:
        vmx.set("displayname", name)
    if cpu_count is not None:
        vmx.set("numvcpus", cpu_count)
    if cores_per_socket is not None:
        vmx.set("cpuid.corespersocket", cores_per_socket)
    if memory_mb is not None:
        vmx.set("memsize", memory_mb)
    if annotation is not None:
        vmx.set("annotation", annotation)
    after = {
        "name": vmx.get("displayname"),
        "cpu_count": _as_int(vmx.get("numvcpus")),
        "cores_per_socket": _as_int(vmx.get("cpuid.corespersocket")),
        "memory_mb": _as_int(vmx.get("memsize")),
        "annotation": vmx.get("annotation"),
    }
    return {"previous": before, "current": after}


def _ethernet_adapters(vmx: VmxFile) -> list[dict[str, Any]]:
    adapters = []
    for index in range(10):
        present = vmx.get(f"ethernet{index}.present")
        if present is None:
            continue
        if present.lower() not in {"true", "1", "yes"}:
            continue
        adapters.append(
            {
                "index": index,
                "connection_type": vmx.get(f"ethernet{index}.connectiontype") or "bridged",
                "virtual_dev": vmx.get(f"ethernet{index}.virtualdev"),
                "address_type": vmx.get(f"ethernet{index}.addresstype"),
                "mac_address": vmx.get(f"ethernet{index}.address")
                or vmx.get(f"ethernet{index}.generatedaddress"),
            }
        )
    return adapters


def _virtual_disks(vmx: VmxFile) -> list[dict[str, Any]]:
    disks = []
    for controller in ("scsi", "sata", "nvme", "ide"):
        for bus in range(4):
            for unit in range(16):
                key = f"{controller}{bus}:{unit}.filename"
                filename = vmx.get(key)
                if not filename:
                    continue
                present = vmx.get(f"{controller}{bus}:{unit}.present")
                if present and present.lower() not in {"true", "1", "yes"}:
                    continue
                disks.append(
                    {
                        "controller": f"{controller}{bus}:{unit}",
                        "file": filename,
                        "path": str((vmx.path.parent / filename).resolve())
                        if not Path(filename).is_absolute()
                        else filename,
                    }
                )
    return disks


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _render_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
