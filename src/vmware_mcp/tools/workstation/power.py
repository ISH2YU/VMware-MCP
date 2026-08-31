"""Local VM power control."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer

from ...config import PermissionMode
from .._common import DESTRUCTIVE, ToolContext, mcp_tool

PowerAction = Literal[
    "start", "stop", "reset", "suspend", "pause", "unpause", "hard_stop", "hard_reset"
]


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

        Starting a VM that is already on, or stopping one that is already off,
        returns ``status: no_change`` rather than failing.

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

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_power_many(
        pattern: str,
        action: PowerAction,
        gui: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply one power action to every VM whose name matches ``pattern``.

        This is how you start or stop a whole batch of test VMs in one call,
        e.g. ``pattern="web-test-*"``, ``action="stop"``. VMs are processed
        concurrently and individual failures are reported without stopping the
        rest.

        Run with ``dry_run=true`` first to see exactly which VMs match.

        Requires permission mode ``write`` or higher.

        Args:
            pattern: Name filter. Plain text is a case-insensitive substring;
                ``*`` and ``?`` enable glob matching.
            action: The same actions as ``vmware_power_vm``.
            gui: Open a window per VM when starting. Default headless.
            dry_run: Only report which VMs match; change nothing.
        """
        if not dry_run:
            settings.require(PermissionMode.WRITE, f"vmware_power_many({action})")
        return await client.power_many(pattern, action, gui=gui, dry_run=dry_run)
