"""Local VM snapshot tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ...config import PermissionMode
from .._common import ToolContext, mcp_tool, require_non_empty

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_list_snapshots(vm: str) -> dict[str, Any]:
        """List the snapshots of a local virtual machine.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
        """
        snapshots = await client.list_snapshots(vm)
        resolved = client.resolve(vm)
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
            name: Name for the new snapshot.
        """
        settings.require(PermissionMode.WRITE, "vmware_create_snapshot")
        return await client.create_snapshot(vm, require_non_empty(name, "name"))

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_revert_snapshot(vm: str, snapshot: str) -> dict[str, Any]:
        """Revert a local virtual machine to a snapshot, discarding later changes.

        Requires permission mode ``destructive``.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            snapshot: Snapshot name.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vmware_revert_snapshot")
        return await client.revert_snapshot(vm, require_non_empty(snapshot, "snapshot"))

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
        return await client.delete_snapshot(
            vm, require_non_empty(snapshot, "snapshot"), delete_children=delete_children
        )
