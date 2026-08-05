"""Local Workstation / Fusion inventory and info tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ...config import Settings
from .._common import ToolContext, mcp_tool, paginate

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings: Settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_about() -> dict[str, Any]:
        """Identify the local VMware product and what this server can see.

        Returns the product (Workstation / Fusion / Player), the path to
        ``vmrun``, how many VMs were found under the configured directories, how
        many are running, whether guest credentials are configured, and the
        active permission mode. Call this first.
        """
        return await client.about()

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_list_vms(
        name: str | None = None,
        guest_os: str | None = None,
        guest_os_family: str | None = None,
        running_only: bool = False,
        powered_off_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List local virtual machines found under the configured VM directories.

        Args:
            name: Filter on display name. Plain text is a case-insensitive
                substring; ``*`` and ``?`` enable glob matching.
            guest_os: Filter on the VMware guest OS id, e.g. ``windows11`` or
                ``ubuntu``.
            guest_os_family: Coarse family: ``windows``, ``linux``, ``macos``.
            running_only: Only return VMs that are currently powered on.
            powered_off_only: Only return VMs that are powered off.
            limit: Maximum number of VMs to return.
            offset: Number of matches to skip, for paging.
        """
        vms = await client.list_vms(
            name=name,
            guest_os=guest_os,
            guest_os_family=guest_os_family,
            running_only=running_only,
            powered_off_only=powered_off_only,
        )
        page, meta = paginate(vms, limit=limit, offset=offset, settings=settings)
        return {**meta, "vms": page}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_get_vm(vm: str) -> dict[str, Any]:
        """Full detail for one local virtual machine.

        Includes CPU, memory, guest OS, network adapters, virtual disks,
        power state, VMware Tools state, guest IP (when running) and the
        snapshot list.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
        """
        return {"vm": await client.get_vm(vm)}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_list_running() -> dict[str, Any]:
        """List the ``.vmx`` paths of every VM currently powered on."""
        paths = await client.list_running()
        return {"count": len(paths), "vms": paths}
