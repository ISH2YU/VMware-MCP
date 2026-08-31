"""Model Context Protocol server for local VMware Workstation / Fusion / Player."""

from __future__ import annotations

from typing import Any

__version__ = "0.2.0"

__all__ = ["__version__", "create_server", "load_settings"]


def __getattr__(name: str) -> Any:  # pragma: no cover - thin lazy import shim
    if name == "create_server":
        from .server import create_server

        return create_server
    if name == "load_settings":
        from .config import load_settings

        return load_settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
