"""Tool modules, grouped by the part of vSphere they cover."""

from __future__ import annotations

from mcp.server import MCPServer

from . import inventory, lifecycle, monitoring, networking, power, snapshots, storage, vms
from ._common import ToolContext

#: Registration order determines the order tools are listed to clients.
MODULES = (inventory, vms, storage, networking, monitoring, power, snapshots, lifecycle)


def register_all(server: MCPServer, context: ToolContext) -> None:
    for module in MODULES:
        module.register(server, context)


__all__ = ["MODULES", "ToolContext", "register_all"]
