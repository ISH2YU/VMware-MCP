"""Tool modules for whichever backend is configured."""

from __future__ import annotations

from mcp.server import MCPServer

from ._common import ToolContext


def register_all(server: MCPServer, context: ToolContext) -> None:
    from ..config import Backend, WorkstationSettings

    if isinstance(context.settings, WorkstationSettings) or (
        getattr(context.settings, "backend", None) is Backend.WORKSTATION
    ):
        from . import workstation

        workstation.register_all(server, context)
    else:
        from . import vsphere

        vsphere.register_all(server, context)


__all__ = ["ToolContext", "register_all"]
