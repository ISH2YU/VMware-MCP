"""Tool modules for local VMware Workstation / Fusion / Player."""

from __future__ import annotations

from ._common import ToolContext
from .workstation import MODULES, register_all

__all__ = ["MODULES", "ToolContext", "register_all"]
