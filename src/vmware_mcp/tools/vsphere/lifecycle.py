"""Virtual machine lifecycle: clone, reconfigure, migrate and delete."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...config import PermissionMode
from ...errors import InvalidArgumentError
from ...vsphere import lookup, mappers
from ...vsphere.query import moid_of
from ...vsphere.tasks import run_task
from .._common import ToolContext, mcp_tool, require_non_empty

MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)

MigrationPriority = Literal["default", "high", "low"]

_PRIORITY = {
    "default": vim.VirtualMachine.MovePriority.defaultPriority,
    "high": vim.VirtualMachine.MovePriority.highPriority,
    "low": vim.VirtualMachine.MovePriority.lowPriority,
}


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=MUTATING)
    async def vsphere_clone_vm(
        vm: str,
        name: str,
        ctx: Context,
        host: str | None = None,
        datastore: str | None = None,
        resource_pool: str | None = None,
        folder: str | None = None,
        power_on: bool = False,
        as_template: bool = False,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Clone a virtual machine or deploy one from a template.

        Placement defaults to the source VM's host, datastore, resource pool and
        folder; override any of them individually. Cloning copies every disk, so
        it can take a long time for large VMs -- pass ``wait=false`` to get a
        task id back immediately and poll it with ``vsphere_get_task``.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Source VM or template (name, moid, UUID or inventory path).
            name: Name for the new virtual machine.
            host: Target ESXi host.
            datastore: Target datastore for the clone's files.
            resource_pool: Target resource pool.
            folder: Target VM folder.
            power_on: Power the clone on once the clone completes.
            as_template: Register the clone as a template instead of a VM.
            wait: Wait for the clone task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.WRITE, "vsphere_clone_vm")
        clone_name = require_non_empty(name, "name")
        if power_on and as_template:
            raise InvalidArgumentError("A template cannot be powered on; set one or the other.")

        index = await client.path_index()
        source = await client.resolve(
            lookup.VM, vm, index=index, extra_properties=("resourcePool", "datastore")
        )
        placement = await _resolve_placement(
            client, index, source, host, datastore, resource_pool, folder
        )
        source_moid = source.moid

        def start(service_instance: vim.ServiceInstance) -> Any:
            relocate_fields: dict[str, Any] = {}
            if placement.host_moid:
                relocate_fields["host"] = lookup.managed_object(
                    service_instance, vim.HostSystem, placement.host_moid
                )
            if placement.datastore_moid:
                relocate_fields["datastore"] = lookup.managed_object(
                    service_instance, vim.Datastore, placement.datastore_moid
                )
            if placement.resource_pool_moid:
                relocate_fields["pool"] = lookup.managed_object(
                    service_instance, vim.ResourcePool, placement.resource_pool_moid
                )
            clone_spec = vim.vm.CloneSpec(
                location=vim.vm.RelocateSpec(**relocate_fields),
                powerOn=power_on,
                template=as_template,
            )
            target_folder = lookup.managed_object(
                service_instance, vim.Folder, placement.folder_moid
            )
            source_vm = lookup.managed_object(service_instance, vim.VirtualMachine, source_moid)
            return source_vm.CloneVM_Task(folder=target_folder, name=clone_name, spec=clone_spec)

        return await run_task(
            client,
            start,
            operation=f"clone {source.get('name')} to {clone_name}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "source_vm": source.get("name"),
                "source_moid": source_moid,
                "new_vm": clone_name,
                "placement": asdict(placement),
                "power_on": power_on,
                "template": as_template,
            },
        )

    @mcp_tool(server, annotations=MUTATING)
    async def vsphere_reconfigure_vm(
        vm: str,
        ctx: Context,
        cpu_count: int | None = None,
        cores_per_socket: int | None = None,
        memory_mb: int | None = None,
        annotation: str | None = None,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Change a virtual machine's CPU count, memory size or notes.

        Unless CPU/memory hot-add is enabled on the guest, vSphere requires the
        VM to be powered off for resource changes and will reject the request
        otherwise. Only the supplied fields are changed.

        Requires permission mode ``write`` or higher.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            cpu_count: New total number of virtual CPUs.
            cores_per_socket: Cores per virtual socket. Must divide ``cpu_count``.
            memory_mb: New memory size in MiB.
            annotation: Replacement notes/annotation text.
            wait: Wait for the reconfigure task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.WRITE, "vsphere_reconfigure_vm")
        if (
            cpu_count is None
            and memory_mb is None
            and annotation is None
            and cores_per_socket is None
        ):
            raise InvalidArgumentError(
                "Nothing to change: supply at least one of cpu_count, cores_per_socket, "
                "memory_mb or annotation."
            )
        if cpu_count is not None and cpu_count < 1:
            raise InvalidArgumentError("cpu_count must be at least 1.")
        if memory_mb is not None and memory_mb < 4:
            raise InvalidArgumentError("memory_mb must be at least 4.")
        if cores_per_socket is not None:
            if cores_per_socket < 1:
                raise InvalidArgumentError("cores_per_socket must be at least 1.")
            if cpu_count is not None and cpu_count % cores_per_socket != 0:
                raise InvalidArgumentError(
                    f"cpu_count ({cpu_count}) must be a multiple of cores_per_socket "
                    f"({cores_per_socket})."
                )

        index = await client.path_index()
        record = await client.resolve(
            lookup.VM,
            vm,
            index=index,
            extra_properties=(
                "runtime.powerState",
                "config.hardware.numCPU",
                "config.hardware.memoryMB",
            ),
        )
        moid = record.moid
        changes = {
            "cpu_count": cpu_count,
            "cores_per_socket": cores_per_socket,
            "memory_mb": memory_mb,
            "annotation": annotation,
        }

        spec_fields: dict[str, Any] = {
            "numCPUs": cpu_count,
            "numCoresPerSocket": cores_per_socket,
            "memoryMB": memory_mb,
            "annotation": annotation,
        }
        spec_fields = {key: value for key, value in spec_fields.items() if value is not None}

        def start(service_instance: vim.ServiceInstance) -> Any:
            target = lookup.managed_object(service_instance, vim.VirtualMachine, moid)
            return target.ReconfigVM_Task(spec=vim.vm.ConfigSpec(**spec_fields))

        return await run_task(
            client,
            start,
            operation=f"reconfigure {record.get('name')}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": record.get("name"),
                "moid": moid,
                "power_state": mappers.as_text(record.props.get("runtime.powerState")),
                "previous": {
                    "cpu_count": record.props.get("config.hardware.numCPU"),
                    "memory_mb": record.props.get("config.hardware.memoryMB"),
                },
                "requested": {key: value for key, value in changes.items() if value is not None},
            },
        )

    @mcp_tool(server, annotations=MUTATING)
    async def vsphere_migrate_vm(
        vm: str,
        ctx: Context,
        host: str | None = None,
        datastore: str | None = None,
        resource_pool: str | None = None,
        priority: MigrationPriority = "default",
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Move a virtual machine to another host, datastore or resource pool.

        Supplying ``host`` performs a vMotion, ``datastore`` a storage vMotion,
        and both at once a combined migration. At least one target is required.

        Requires permission mode ``write`` or higher.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            host: Destination ESXi host.
            datastore: Destination datastore.
            resource_pool: Destination resource pool.
            priority: vMotion scheduling priority.
            wait: Wait for the migration task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.WRITE, "vsphere_migrate_vm")
        if host is None and datastore is None and resource_pool is None:
            raise InvalidArgumentError(
                "Specify at least one of host, datastore or resource_pool to migrate to."
            )

        index = await client.path_index()
        record = await client.resolve(lookup.VM, vm, index=index)
        moid = record.moid
        host_moid = (await client.resolve(lookup.HOST, host, index=index)).moid if host else None
        datastore_moid = (
            (await client.resolve(lookup.DATASTORE, datastore, index=index)).moid
            if datastore
            else None
        )
        pool_moid = (
            (await client.resolve(lookup.RESOURCE_POOL, resource_pool, index=index)).moid
            if resource_pool
            else None
        )

        def start(service_instance: vim.ServiceInstance) -> Any:
            spec = vim.vm.RelocateSpec()
            if host_moid:
                spec.host = lookup.managed_object(service_instance, vim.HostSystem, host_moid)
            if datastore_moid:
                spec.datastore = lookup.managed_object(
                    service_instance, vim.Datastore, datastore_moid
                )
            if pool_moid:
                spec.pool = lookup.managed_object(service_instance, vim.ResourcePool, pool_moid)
            target = lookup.managed_object(service_instance, vim.VirtualMachine, moid)
            return target.RelocateVM_Task(spec=spec, priority=_PRIORITY[priority])

        return await run_task(
            client,
            start,
            operation=f"migrate {record.get('name')}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": record.get("name"),
                "moid": moid,
                "target_host": host,
                "target_datastore": datastore,
                "target_resource_pool": resource_pool,
            },
        )

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vsphere_delete_vm(
        vm: str,
        confirm: bool,
        ctx: Context,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Permanently delete a virtual machine and all of its files from disk.

        This cannot be undone. The VM must be powered off. Requires permission
        mode ``destructive`` and an explicit ``confirm=true``.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            confirm: Must be ``true``; guards against accidental deletion.
            wait: Wait for the delete task to finish.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vsphere_delete_vm")
        if not confirm:
            raise InvalidArgumentError(
                "Refusing to delete a VM without confirm=true. Deleting a VM removes all of "
                "its virtual disks permanently."
            )

        index = await client.path_index()
        record = await client.resolve(
            lookup.VM, vm, index=index, extra_properties=("runtime.powerState", "config.template")
        )
        power_state = mappers.as_text(record.props.get("runtime.powerState"))
        if power_state == "poweredOn":
            raise InvalidArgumentError(
                f"{record.get('name')!r} is powered on. Power it off before deleting it."
            )
        moid = record.moid

        def start(service_instance: vim.ServiceInstance) -> Any:
            target = lookup.managed_object(service_instance, vim.VirtualMachine, moid)
            return target.Destroy_Task()

        return await run_task(
            client,
            start,
            operation=f"delete {record.get('name')}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result={
                "vm": record.get("name"),
                "moid": moid,
                "was_template": bool(record.props.get("config.template")),
            },
        )


@dataclass(frozen=True)
class ClonePlacement:
    """Where a clone will be created. Only the folder is mandatory."""

    folder_moid: str
    host_moid: str | None = None
    datastore_moid: str | None = None
    resource_pool_moid: str | None = None


async def _resolve_placement(
    client: Any,
    index: Any,
    source: Any,
    host: str | None,
    datastore: str | None,
    resource_pool: str | None,
    folder: str | None,
) -> ClonePlacement:
    """Work out where a clone should land, defaulting to the source's placement."""
    host_moid = (await client.resolve(lookup.HOST, host, index=index)).moid if host else None
    datastore_moid = (
        (await client.resolve(lookup.DATASTORE, datastore, index=index)).moid if datastore else None
    )
    if resource_pool:
        pool_moid: str | None = (
            await client.resolve(lookup.RESOURCE_POOL, resource_pool, index=index)
        ).moid
    else:
        # Templates have no resource pool of their own, so fall back to the pool
        # of the target host's cluster before giving up.
        pool_moid = moid_of(source.props.get("resourcePool"))
        if pool_moid is None and host_moid is not None:
            pool_moid = await _pool_of_host(client, host_moid)
        if pool_moid is None:
            raise InvalidArgumentError(
                "The source has no resource pool of its own (templates never do). "
                "Pass resource_pool, or host so the cluster's root pool can be used."
            )

    folder_moid = (
        (await client.resolve(lookup.FOLDER, folder, index=index)).moid
        if folder
        else moid_of(source.props.get("parent"))
    )
    if folder_moid is None:
        raise InvalidArgumentError(
            "Could not determine a target folder for the clone; pass folder explicitly."
        )
    return ClonePlacement(
        folder_moid=folder_moid,
        host_moid=host_moid,
        datastore_moid=datastore_moid,
        resource_pool_moid=pool_moid,
    )


async def _pool_of_host(client: Any, host_moid: str) -> str | None:
    """The root resource pool of the cluster (or standalone host) owning ``host_moid``."""
    host_record = await client.properties_for(vim.HostSystem, host_moid, ("parent",))
    compute_moid = moid_of(host_record.props.get("parent"))
    if compute_moid is None:
        return None
    compute = await client.properties_for(vim.ComputeResource, compute_moid, ("resourcePool",))
    return moid_of(compute.props.get("resourcePool"))
