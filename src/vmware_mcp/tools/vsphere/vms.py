"""Read-only virtual machine tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...vsphere import lookup, mappers
from ...vsphere.query import moid_of
from .._common import ToolContext, equals_any, mcp_tool, name_matches, paginate, sort_by_name

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_vms(
        name: str | None = None,
        power_state: str | None = None,
        datacenter: str | None = None,
        cluster: str | None = None,
        host: str | None = None,
        guest_os: str | None = None,
        ip_address: str | None = None,
        include_templates: bool = False,
        only_templates: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List virtual machines with power state, resources and guest details.

        All filters are combined with AND. Results are capped; check the
        ``truncated`` field and page with ``offset`` when it is true.

        Args:
            name: VM name filter. Plain text matches as a case-insensitive
                substring; ``*`` and ``?`` enable glob matching.
            power_state: ``poweredOn``, ``poweredOff`` or ``suspended``.
            datacenter: Only VMs in this datacenter.
            cluster: Only VMs whose host belongs to this cluster.
            host: Only VMs registered to this ESXi host (name or moid).
            guest_os: Filter on the configured guest OS name, e.g. ``Ubuntu``.
            ip_address: Filter on the primary guest IP address (substring match).
            include_templates: Include VM templates alongside real VMs.
            only_templates: Return only VM templates.
            limit: Maximum number of VMs to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.VirtualMachine, mappers.VM_SUMMARY_PROPERTIES)
        vms = [mappers.map_vm_summary(record, index) for record in records]

        host_names = None
        if host is not None:
            host_record = await client.resolve(lookup.HOST, host, index=index)
            host_names = {host_record.moid}

        cluster_hosts: set[str] | None = None
        if cluster is not None:
            host_records = await client.collect(vim.HostSystem, ("name", "parent"))
            cluster_hosts = {
                record.moid
                for record in host_records
                if index.name_of(moid_of(record.props.get("parent"))) == cluster
            }

        def keep(vm: dict[str, Any]) -> bool:
            if only_templates and not vm["is_template"]:
                return False
            if not include_templates and not only_templates and vm["is_template"]:
                return False
            if not name_matches(vm["name"], name):
                return False
            if not equals_any(vm["power_state"], [power_state] if power_state else None):
                return False
            if datacenter is not None and vm["datacenter"] != datacenter:
                return False
            if host_names is not None and vm["host_moid"] not in host_names:
                return False
            if cluster_hosts is not None and vm["host_moid"] not in cluster_hosts:
                return False
            if guest_os is not None and not name_matches(vm["guest_os"], guest_os):
                return False
            return ip_address is None or name_matches(vm["ip_address"], ip_address)

        filtered = [vm for vm in vms if keep(vm)]
        page, meta = paginate(sort_by_name(filtered), limit=limit, offset=offset, settings=settings)
        return {**meta, "vms": page}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_get_vm(vm: str) -> dict[str, Any]:
        """Full detail for one virtual machine.

        Includes virtual hardware (disks and network adapters), guest
        networking and filesystem usage reported by VMware Tools, resource
        allocations, and the snapshot tree.

        Args:
            vm: VM name, managed object id (``vm-1024``), BIOS/instance UUID or
                inventory path such as ``/Prod/vm/Tier1/web-01``.
        """
        index = await client.path_index()
        record = await client.resolve(lookup.VM, vm, index=index)
        detailed = await client.properties_for(
            vim.VirtualMachine, record.moid, mappers.VM_DETAIL_PROPERTIES
        )
        return {"vm": mappers.map_vm_detail(detailed, index)}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_get_vm_summary_by_host() -> dict[str, Any]:
        """Aggregate VM counts and allocated resources per ESXi host.

        A quick capacity/placement overview: how many VMs sit on each host, how
        many are powered on, and how much vCPU and memory has been handed out.
        """
        index = await client.path_index()
        host_records = await client.collect(vim.HostSystem, mappers.HOST_PROPERTIES)
        vm_records = await client.collect(vim.VirtualMachine, mappers.VM_SUMMARY_PROPERTIES)

        buckets: dict[str, dict[str, Any]] = {}
        for record in host_records:
            host = mappers.map_host(record, index)
            buckets[record.moid] = {
                "host": host["name"],
                "host_moid": record.moid,
                "cluster": host["cluster"],
                "connection_state": host["connection_state"],
                "cpu_cores": host["cpu_cores"],
                "memory_gib": host["memory_gib"],
                "vm_count": 0,
                "powered_on_vm_count": 0,
                "allocated_vcpus": 0,
                "allocated_memory_gib": 0.0,
            }

        unassigned = 0
        for record in vm_records:
            vm = mappers.map_vm_summary(record, index)
            if vm["is_template"]:
                continue
            bucket = buckets.get(vm["host_moid"] or "")
            if bucket is None:
                unassigned += 1
                continue
            bucket["vm_count"] += 1
            if vm["power_state"] == "poweredOn":
                bucket["powered_on_vm_count"] += 1
                bucket["allocated_vcpus"] += vm["cpu_count"] or 0
                bucket["allocated_memory_gib"] += vm["memory_gib"] or 0.0

        summaries = sorted(buckets.values(), key=lambda item: -item["vm_count"])
        for summary in summaries:
            summary["allocated_memory_gib"] = round(summary["allocated_memory_gib"], 2)
            summary["vcpu_overcommit_ratio"] = (
                round(summary["allocated_vcpus"] / summary["cpu_cores"], 2)
                if summary["cpu_cores"]
                else None
            )
        return {
            "host_count": len(summaries),
            "unplaced_vm_count": unassigned,
            "hosts": summaries,
        }
