"""Local VM snapshot tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ...config import PermissionMode
from .._common import DESTRUCTIVE, MUTATING, READ_ONLY, ToolContext, mcp_tool


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_list_snapshots(vm: str) -> dict[str, Any]:
        """List the snapshots of a local virtual machine.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
        """
        resolved = await client.resolve_async(vm)
        snapshots = await client.list_snapshots(str(resolved.path))
        return {
            "vm": resolved.name,
            "path": str(resolved.path),
            "count": len(snapshots),
            "snapshots": snapshots,
        }

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_create_snapshot(vm: str, name: str) -> dict[str, Any]:
        """Take a snapshot of a local virtual machine.

        Snapshots are the cheap way to roll a test VM back to a known-good
        state between runs. Prefer linked clones from a clean snapshot when
        spinning up many disposable Windows VMs.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            name: Name for the new snapshot. No path separators.
        """
        settings.require(PermissionMode.WRITE, "vmware_create_snapshot")
        return await client.create_snapshot(vm, name)

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_revert_snapshot(vm: str, snapshot: str) -> dict[str, Any]:
        """Revert a local virtual machine to a snapshot, discarding later changes.

        Requires permission mode ``destructive``.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            snapshot: Snapshot name.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vmware_revert_snapshot")
        return await client.revert_snapshot(vm, snapshot)

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_revert_many(
        pattern: str, snapshot: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Revert every VM matching ``pattern`` back to a snapshot.

        This is the "reset the whole test lab" call: it stops each matching VM
        if it is running, then reverts it. Use ``dry_run=true`` first to confirm
        the match list.

        Requires permission mode ``destructive``.

        Args:
            pattern: Name filter, e.g. ``web-test-*``.
            snapshot: Snapshot name that every matching VM should return to.
            dry_run: Only report which VMs match; change nothing.
        """
        if not dry_run:
            settings.require(PermissionMode.DESTRUCTIVE, "vmware_revert_many")
        return await client.revert_many(pattern, snapshot, dry_run=dry_run)

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_delete_snapshot(
        vm: str, snapshot: str, delete_children: bool = False
    ) -> dict[str, Any]:
        """Delete a snapshot from a local virtual machine.

        Requires permission mode ``destructive``.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            snapshot: Snapshot name.
            delete_children: Also delete snapshots taken from this one.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vmware_delete_snapshot")
        return await client.delete_snapshot(vm, snapshot, delete_children=delete_children)
