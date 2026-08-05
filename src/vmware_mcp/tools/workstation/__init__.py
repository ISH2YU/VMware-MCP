"""Workstation / Fusion / Player tool modules."""

from __future__ import annotations

from mcp.server import MCPServer

from .._common import ToolContext
from . import guest, inventory, lifecycle, power, snapshots

MODULES = (inventory, power, snapshots, lifecycle, guest)


def register_all(server: MCPServer, context: ToolContext) -> None:
    for module in MODULES:
        module.register(server, context)


__all__ = ["MODULES", "register_all"]
