"""Snapshot inspection and management."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...config import PermissionMode
from ...errors import AmbiguousObjectError, ObjectNotFoundError
from ...vsphere import lookup, mappers
from ...vsphere.tasks import run_task
from .._common import ToolContext, mcp_tool, require_non_empty

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    async def _snapshot_state(vm: str) -> tuple[Any, dict[str, Any] | None]:
        index = await client.path_index()
        record = await client.resolve(lookup.VM, vm, index=index)
        detailed = await client.properties_for(
            vim.VirtualMachine, record.moid, ("name", "snapshot")
        )
        return detailed, mappers.map_snapshot_info(detailed.props.get("snapshot"))

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_snapshots(vm: str) -> dict[str, Any]:
        """List the snapshots of a virtual machine as both a tree and a flat list.

        Each snapshot carries the ``moid`` needed to revert to or delete it, and
        ``is_current`` marks the snapshot the VM is currently running from.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
        """
        record, snapshots = await _snapshot_state(vm)
        if snapshots is None:
            return {
                "vm": record.get("name"),
                "moid": record.moid,
                "count": 0,
                "snapshots": [],
                "tree": [],
            }
        return {
            "vm": record.get("name"),
            "moid": record.moid,
            "count": snapshots["count"],
            "current_snapshot_moid": snapshots["current_snapshot_moid"],
            "snapshots": mappers.flatten_snapshots(snapshots["tree"]),
            "tree": snapshots["tree"],
        }

    @mcp_tool(server, annotations=MUTATING)
    async def vsphere_create_snapshot(
        vm: str,
        name: str,
        ctx: Context,
        description: str | None = None,
        include_memory: bool = False,
        quiesce: bool = False,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Take a snapshot of a virtual machine.

        Snapshots are not backups: they grow with every write and degrade
        performance if left in place, so delete them once they are no longer
        needed.

        Requires permission mode ``write`` or higher.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            name: Name for the new snapshot.
            description: Optional free-text description.
            include_memory: Capture the memory state too, so a revert returns
                the VM to a running state. Slower and larger.
            quiesce: Ask VMware Tools to quiesce the guest filesystem for a
                crash-consistent snapshot. Requires VMware Tools.
            wait: Wait for the snapshot task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.WRITE, "vsphere_create_snapshot")
        snapshot_name = require_non_empty(name, "name")

        index = await client.path_index()
        record = await client.resolve(lookup.VM, vm, index=index)
        moid = record.moid

        def start(service_instance: vim.ServiceInstance) -> Any:
            target = lookup.managed_object(service_instance, vim.VirtualMachine, moid)
            return target.CreateSnapshot_Task(
                name=snapshot_name,
                description=description or "",
                memory=include_memory,
                quiesce=quiesce,
            )

        return await run_task(
            client,
            start,
            operation=f"create snapshot {snapshot_name!r} on {record.get('name')}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": record.get("name"),
                "moid": moid,
                "snapshot_name": snapshot_name,
                "include_memory": include_memory,
                "quiesced": quiesce,
            },
        )

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vsphere_revert_to_snapshot(
        vm: str,
        ctx: Context,
        snapshot: str | None = None,
        suppress_power_on: bool = False,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Revert a virtual machine to a snapshot, discarding later changes.

        Everything written since the snapshot was taken is lost. Requires
        permission mode ``destructive``.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            snapshot: Snapshot name, snapshot path (``parent/child``) or
                snapshot moid. Defaults to the VM's current snapshot.
            suppress_power_on: Leave the VM powered off after reverting.
            wait: Wait for the revert task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vsphere_revert_to_snapshot")
        record, snapshots = await _snapshot_state(vm)
        target = _select_snapshot(snapshots, snapshot, record.get("name"))
        vm_moid = record.moid
        snapshot_moid = target["moid"]

        def start(service_instance: vim.ServiceInstance) -> Any:
            snapshot_ref = lookup.managed_object(service_instance, vim.vm.Snapshot, snapshot_moid)
            return snapshot_ref.RevertToSnapshot_Task(suppressPowerOn=suppress_power_on)

        return await run_task(
            client,
            start,
            operation=f"revert {record.get('name')} to snapshot {target['name']!r}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": record.get("name"),
                "moid": vm_moid,
                "snapshot": target["name"],
                "snapshot_moid": snapshot_moid,
            },
        )

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vsphere_delete_snapshot(
        vm: str,
        ctx: Context,
        snapshot: str | None = None,
        remove_children: bool = False,
        delete_all: bool = False,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Delete one snapshot, a snapshot subtree, or every snapshot of a VM.

        Deleting a snapshot consolidates its data into the parent disk; the VM
        keeps all current data. Requires permission mode ``destructive``.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            snapshot: Snapshot name, path or moid. Ignored when ``delete_all``
                is set.
            remove_children: Also delete snapshots taken from this one.
            delete_all: Delete the entire snapshot tree ("Delete All" in the
                vSphere client).
            wait: Wait for the delete task to finish.
            timeout_seconds: Override the default task timeout. Consolidation of
                large snapshots can take a long time.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vsphere_delete_snapshot")
        record, snapshots = await _snapshot_state(vm)
        vm_moid = record.moid
        vm_name = record.get("name")

        if delete_all:
            if not snapshots or snapshots["count"] == 0:
                raise ObjectNotFoundError(f"{vm_name!r} has no snapshots to delete.")

            def start_all(service_instance: vim.ServiceInstance) -> Any:
                target = lookup.managed_object(service_instance, vim.VirtualMachine, vm_moid)
                return target.RemoveAllSnapshots_Task()

            return await run_task(
                client,
                start_all,
                operation=f"delete all snapshots of {vm_name}",
                wait=wait,
                timeout=timeout_seconds,
                reporter=ctx,
                result={"vm": vm_name, "moid": vm_moid, "deleted": snapshots["count"]},
            )

        target_snapshot = _select_snapshot(snapshots, snapshot, vm_name)
        snapshot_moid = target_snapshot["moid"]

        def start(service_instance: vim.ServiceInstance) -> Any:
            snapshot_ref = lookup.managed_object(service_instance, vim.vm.Snapshot, snapshot_moid)
            return snapshot_ref.RemoveSnapshot_Task(removeChildren=remove_children)

        return await run_task(
            client,
            start,
            operation=f"delete snapshot {target_snapshot['name']!r} of {vm_name}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": vm_name,
                "moid": vm_moid,
                "snapshot": target_snapshot["name"],
                "snapshot_moid": snapshot_moid,
                "removed_children": remove_children,
            },
        )


def _select_snapshot(
    snapshots: dict[str, Any] | None, identifier: str | None, vm_name: str | None
) -> dict[str, Any]:
    """Pick one snapshot by moid, path or name, or fall back to the current one."""
    if not snapshots or not snapshots["tree"]:
        raise ObjectNotFoundError(f"{vm_name!r} has no snapshots.")
    flattened = mappers.flatten_snapshots(snapshots["tree"])

    if identifier is None:
        current = snapshots["current_snapshot_moid"]
        for node in flattened:
            if node["moid"] == current:
                return node
        raise ObjectNotFoundError(
            f"{vm_name!r} has snapshots but no current snapshot; name the snapshot explicitly."
        )

    needle = identifier.strip()
    lowered = needle.lower()
    for tier in (
        [node for node in flattened if node["moid"] == needle],
        [node for node in flattened if (node["path"] or "").lower() == lowered.strip("/")],
        [node for node in flattened if (node["name"] or "").lower() == lowered],
    ):
        if len(tier) == 1:
            return tier[0]
        if len(tier) > 1:
            paths = ", ".join(node["path"] for node in tier)
            raise AmbiguousObjectError(
                f"{len(tier)} snapshots of {vm_name!r} match {identifier!r}: {paths}. "
                f"Use the snapshot path or moid."
            )
    available = ", ".join(node["path"] for node in flattened) or "none"
    raise ObjectNotFoundError(
        f"No snapshot of {vm_name!r} matches {identifier!r}. Available snapshots: {available}."
    )
