"""Path and name checks so the server stays inside the directories it was given.

Two separate sandboxes live here:

* the **VM library** (``VMWARE_VM_DIRS``) bounds which ``.vmx`` files may be
  touched at all, and
* the **host I/O allow-lists** (``VMWARE_HOST_READ_DIRS`` /
  ``VMWARE_HOST_WRITE_DIRS``) bound where guest file transfers and screenshots
  may read from and write to on the machine running the server.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from ..errors import InvalidArgumentError, ObjectNotFoundError

# Characters that are illegal in a Windows file name, plus Unix separators.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Device names Windows refuses to use as a file name, whatever the extension.
_RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)

MAX_NAME_LENGTH = 80


def normalize_path(path: Path | str) -> str:
    """Absolute path string, case-normalized on Windows so comparisons stick."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    text = str(resolved)
    return os.path.normcase(text) if sys.platform == "win32" else text


def path_is_within(path: Path | str, root: Path | str) -> bool:
    """True when ``path`` is ``root`` or a descendant, after resolving symlinks."""
    try:
        resolved = Path(path).expanduser().resolve()
        base = Path(root).expanduser().resolve()
    except OSError:
        return False
    if sys.platform == "win32":
        resolved_s = os.path.normcase(str(resolved))
        base_s = os.path.normcase(str(base)).rstrip(os.sep)
        return resolved_s == base_s or resolved_s.startswith(base_s + os.sep)
    if resolved == base:
        return True
    return base in resolved.parents


def path_is_within_any(path: Path | str, roots: Sequence[Path | str]) -> bool:
    return any(path_is_within(path, root) for root in roots)


def _describe(roots: Sequence[Path | str]) -> str:
    return ", ".join(str(Path(root).expanduser()) for root in roots) or "(none configured)"


def require_within_vm_dirs(path: Path | str, roots: Sequence[Path | str], *, what: str) -> Path:
    """Resolve ``path`` and raise if it is not under a configured VM directory."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError as exc:
        raise InvalidArgumentError(f"Cannot resolve {what} path {path}: {exc}") from exc
    if path_is_within_any(resolved, roots):
        return resolved
    raise ObjectNotFoundError(
        f"Refusing to use {what} at {resolved}: it is outside the configured VM "
        f"directories ({_describe(roots)}). Add the folder to VMWARE_VM_DIRS if you "
        f"intend to manage this VM."
    )


def require_host_read(path: Path | str, roots: Sequence[Path | str]) -> Path:
    """Validate a host path the server is about to read. Empty ``roots`` allows any."""
    resolved = Path(path).expanduser().resolve()
    if roots and not path_is_within_any(resolved, roots):
        raise InvalidArgumentError(
            f"Refusing to read {resolved}: it is outside VMWARE_HOST_READ_DIRS "
            f"({_describe(roots)}). Set VMWARE_HOST_READ_DIRS=* to allow any path."
        )
    return resolved


def require_host_write(path: Path | str, roots: Sequence[Path | str]) -> Path:
    """Validate a host path the server is about to create or overwrite."""
    resolved = Path(path).expanduser().resolve()
    if roots and not path_is_within_any(resolved, roots):
        raise InvalidArgumentError(
            f"Refusing to write {resolved}: it is outside VMWARE_HOST_WRITE_DIRS "
            f"({_describe(roots)}). Set VMWARE_HOST_WRITE_DIRS to widen this, or "
            f"VMWARE_HOST_WRITE_DIRS=* to allow any path."
        )
    return resolved


def validate_vm_name(name: str, *, field: str = "name") -> str:
    """A display / folder name that cannot escape the destination directory."""
    stripped = name.strip()
    if not stripped or stripped in {".", ".."}:
        raise InvalidArgumentError(f"{field} must not be empty.")
    if stripped.startswith("."):
        raise InvalidArgumentError(f"{field} must not start with a dot.")
    if len(stripped) > MAX_NAME_LENGTH:
        raise InvalidArgumentError(f"{field} must be at most {MAX_NAME_LENGTH} characters.")
    if _UNSAFE_NAME.search(stripped) or ".." in stripped:
        raise InvalidArgumentError(
            f"{field} contains a reserved or path character. Use letters, numbers, "
            f"spaces, dots, underscores or hyphens."
        )
    if stripped.endswith((" ", ".")):
        raise InvalidArgumentError(f"{field} must not end with a space or a dot.")
    if stripped.split(".")[0].lower() in _RESERVED_WINDOWS_NAMES:
        raise InvalidArgumentError(f"{field} is a name Windows reserves for a device.")
    return stripped


def validate_snapshot_name(name: str, *, field: str = "snapshot") -> str:
    """Snapshot names are free-form but must not look like a path."""
    stripped = name.strip()
    if not stripped:
        raise InvalidArgumentError(f"{field} must not be empty.")
    if any(sep in stripped for sep in ("/", "\\")) or ".." in stripped:
        raise InvalidArgumentError(f"{field} must not contain path separators.")
    if len(stripped) > MAX_NAME_LENGTH:
        raise InvalidArgumentError(f"{field} must be at most {MAX_NAME_LENGTH} characters.")
    return stripped


def validate_guest_path(path: str, *, field: str = "guest_path") -> str:
    """A path inside the guest. Kept permissive; only obvious mistakes are rejected."""
    stripped = path.strip()
    if not stripped:
        raise InvalidArgumentError(f"{field} must not be empty.")
    if "\x00" in stripped:
        raise InvalidArgumentError(f"{field} must not contain a null byte.")
    return stripped


__all__ = [
    "MAX_NAME_LENGTH",
    "normalize_path",
    "path_is_within",
    "path_is_within_any",
    "require_host_read",
    "require_host_write",
    "require_within_vm_dirs",
    "validate_guest_path",
    "validate_snapshot_name",
    "validate_vm_name",
]
