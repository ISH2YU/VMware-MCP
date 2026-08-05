"""Local VM power control."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ...config import PermissionMode
from .._common import ToolContext, mcp_tool

PowerAction = Literal[
    "start", "stop", "reset", "suspend", "pause", "unpause", "hard_stop", "hard_reset"
]

DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_power_vm(
        vm: str,
        action: PowerAction,
        gui: bool = False,
    ) -> dict[str, Any]:
        """Change the power state of a local virtual machine.

        Prefer soft ``stop`` / ``reset`` when VMware Tools is running — they ask
        the guest OS to shut down cleanly. ``hard_stop`` and ``hard_reset`` are
        the equivalent of pulling the plug.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            action: ``start``, ``stop``, ``reset``, ``suspend``, ``pause``,
                ``unpause``, ``hard_stop`` or ``hard_reset``.
            gui: When starting, open the Workstation/Fusion UI. Default is
                headless (``nogui``), which is what you want for batch testing.
        """
        settings.require(PermissionMode.WRITE, f"vmware_power_vm({action})")
        return await client.change_power(vm, action, gui=gui)
