"""Path and name checks so the server stays inside the configured VM library."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from ..errors import InvalidArgumentError, ObjectNotFoundError

# Characters that are illegal in a Windows file name, plus Unix separators.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_path(path: Path | str) -> str:
    """Absolute path string, case-normalized on Windows so comparisons stick."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    text = str(resolved)
    if sys.platform == "win32":
        return os.path.normcase(text)
    return text


def path_is_within(path: Path | str, root: Path | str) -> bool:
    """True when ``path`` is ``root`` or a descendant, after resolving symlinks."""
    try:
        resolved = Path(path).expanduser().resolve()
        base = Path(root).expanduser().resolve()
    except OSError:
        return False
    if sys.platform == "win32":
        resolved_s = os.path.normcase(str(resolved))
        base_s = os.path.normcase(str(base))
        return resolved_s == base_s or resolved_s.startswith(base_s + os.sep)
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        return False


def path_is_within_any(path: Path | str, roots: Sequence[Path | str]) -> bool:
    return any(path_is_within(path, root) for root in roots)


def require_within_vm_dirs(path: Path | str, roots: Sequence[Path | str], *, what: str) -> Path:
    """Resolve ``path`` and raise if it is not under a configured VM directory."""
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError as exc:
        raise InvalidArgumentError(f"Cannot resolve {what} path {path}: {exc}") from exc
    if path_is_within_any(resolved, roots):
        return resolved
    listed = ", ".join(str(Path(root).expanduser()) for root in roots) or "(none configured)"
    raise ObjectNotFoundError(
        f"Refusing to use {what} at {resolved}: it is outside the configured VM "
        f"directories ({listed}). Add the folder to VMWARE_VM_DIRS if you intend "
        f"to manage this VM."
    )


def validate_vm_name(name: str, *, field: str = "name") -> str:
    """A display / folder name that cannot escape the destination directory."""
    stripped = name.strip()
    if not stripped or stripped in {".", ".."}:
        raise InvalidArgumentError(f"{field} must not be empty.")
    if stripped.startswith("."):
        raise InvalidArgumentError(f"{field} must not start with a dot.")
    if _UNSAFE_NAME.search(stripped) or ".." in stripped:
        raise InvalidArgumentError(
            f"{field} contains a reserved or path character. Use letters, numbers, "
            f"spaces, dots, underscores or hyphens."
        )
    return stripped


def validate_snapshot_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise InvalidArgumentError("snapshot name must not be empty.")
    if any(sep in stripped for sep in ("/", "\\")) or ".." in stripped:
        raise InvalidArgumentError("snapshot name must not contain path separators.")
    return stripped
