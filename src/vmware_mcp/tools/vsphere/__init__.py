"""vSphere (vCenter / ESXi) tool modules."""

from __future__ import annotations

from mcp.server import MCPServer

from .._common import ToolContext
from . import inventory, lifecycle, monitoring, networking, power, snapshots, storage, vms

MODULES = (inventory, vms, storage, networking, monitoring, power, snapshots, lifecycle)


def register_all(server: MCPServer, context: ToolContext) -> None:
    for module in MODULES:
        module.register(server, context)


__all__ = ["MODULES", "register_all"]
