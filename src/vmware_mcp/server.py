"""Assembly of the VMware MCP server."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import MCPServer
from pyVmomi import vim

from .config import PermissionMode, Settings, load_settings
from .tools import ToolContext, register_all
from .vsphere import lookup, mappers
from .vsphere.client import VSphereClient

logger = logging.getLogger(__name__)

SERVER_NAME = "vmware-mcp"


def build_instructions(settings: Settings) -> str:
    mode = settings.permission_mode
    if mode is PermissionMode.READ_ONLY:
        capability = (
            "This server is READ-ONLY: inventory, monitoring and performance tools work, "
            "and every tool that would change the environment will refuse."
        )
    elif mode is PermissionMode.WRITE:
        capability = (
            "This server may change the environment: power operations, snapshots, clones, "
            "reconfiguration and migrations are allowed. Deleting VMs and deleting or "
            "reverting snapshots is not."
        )
    else:
        capability = (
            "This server has FULL access, including deleting virtual machines and "
            "reverting or deleting snapshots. Confirm destructive actions with the user "
            "before running them."
        )

    return f"""Tools for VMware vSphere ({settings.endpoint}).

{capability}

Identifying objects: every tool that takes a VM, host, datastore, network or
cluster accepts its name, its managed object id (`vm-1024`, `host-42`), its UUID
where one exists, or its inventory path (`/Prod/vm/Tier1/web-01`). Names are
matched case-insensitively; if several objects share a name the tool reports the
candidates and their moids instead of guessing.

Working effectively:
- Start with `vsphere_about` to see the endpoint, version and permission mode.
- Use `vsphere_search_inventory` when you know a name but not its object type.
- Listings are paginated. When a result has `truncated: true` there is more to
  fetch with `offset`, so never conclude a search from a truncated page.
- Long operations (clone, migrate, snapshot) return a `task_id`. Pass
  `wait=false` for slow work and poll with `vsphere_get_task`.
- Sizes are reported in GiB, memory allocations in MiB and CPU in MHz.
"""


def create_server(
    settings: Settings | None = None, client: VSphereClient | None = None
) -> MCPServer:
    """Build the MCP server, its tools, resources and prompts.

    ``client`` exists so tests can supply a client backed by something other
    than a live vCenter; production callers pass settings only.
    """
    resolved = settings or load_settings()
    vsphere = client or VSphereClient(resolved)
    context = ToolContext(client=vsphere, settings=resolved)

    server = MCPServer(
        name=SERVER_NAME,
        title="VMware vSphere",
        version="0.1.0",
        instructions=build_instructions(resolved),
        website_url="https://github.com/ISH2YU/VMware-MCP",
    )

    register_all(server, context)
    _register_resources(server, context)
    _register_prompts(server)
    return server


def _register_resources(server: MCPServer, context: ToolContext) -> None:
    client = context.client

    @server.resource(
        "vsphere://inventory/summary",
        name="Inventory summary",
        description="Counts and totals for the whole vSphere inventory.",
        mime_type="application/json",
    )
    async def inventory_summary() -> str:
        index = await client.path_index()
        datacenters = await client.collect(vim.Datacenter, mappers.DATACENTER_PROPERTIES)
        clusters = await client.collect(vim.ClusterComputeResource, mappers.CLUSTER_PROPERTIES)
        hosts = await client.collect(vim.HostSystem, mappers.HOST_PROPERTIES)
        vms = await client.collect(vim.VirtualMachine, mappers.VM_SUMMARY_PROPERTIES)
        datastores = await client.collect(vim.Datastore, mappers.DATASTORE_PROPERTIES)

        mapped_vms = [mappers.map_vm_summary(record, index) for record in vms]
        mapped_hosts = [mappers.map_host(record, index) for record in hosts]
        mapped_datastores = [mappers.map_datastore(record, index) for record in datastores]
        capacity = sum(item["capacity_gib"] or 0 for item in mapped_datastores)
        free = sum(item["free_gib"] or 0 for item in mapped_datastores)

        summary: dict[str, Any] = {
            "endpoint": context.settings.endpoint,
            "permission_mode": context.settings.permission_mode.value,
            "datacenters": len(datacenters),
            "clusters": len(clusters),
            "hosts": {
                "total": len(mapped_hosts),
                "connected": sum(
                    1 for host in mapped_hosts if host["connection_state"] == "connected"
                ),
                "in_maintenance": sum(1 for host in mapped_hosts if host["in_maintenance_mode"]),
                "cpu_cores": sum(host["cpu_cores"] or 0 for host in mapped_hosts),
                "memory_gib": round(sum(host["memory_gib"] or 0 for host in mapped_hosts), 2),
            },
            "virtual_machines": {
                "total": sum(1 for vm in mapped_vms if not vm["is_template"]),
                "powered_on": sum(1 for vm in mapped_vms if vm["power_state"] == "poweredOn"),
                "templates": sum(1 for vm in mapped_vms if vm["is_template"]),
                "allocated_vcpus": sum(
                    vm["cpu_count"] or 0 for vm in mapped_vms if not vm["is_template"]
                ),
            },
            "storage": {
                "datastores": len(mapped_datastores),
                "capacity_gib": round(capacity, 2),
                "free_gib": round(free, 2),
                "used_percent": mappers.percent(capacity - free, capacity),
            },
        }
        return json.dumps(summary, indent=2)

    @server.resource(
        "vsphere://vm/{identifier}",
        name="Virtual machine detail",
        description="Full configuration and runtime detail for one virtual machine.",
        mime_type="application/json",
    )
    async def vm_detail(identifier: str) -> str:
        index = await client.path_index()
        record = await client.resolve(lookup.VM, identifier, index=index)
        detailed = await client.properties_for(
            vim.VirtualMachine, record.moid, mappers.VM_DETAIL_PROPERTIES
        )
        return json.dumps(mappers.map_vm_detail(detailed, index), indent=2, default=str)


def _register_prompts(server: MCPServer) -> None:
    @server.prompt(
        name="troubleshoot_vm",
        description="Investigate why a virtual machine is slow, stuck or unreachable.",
    )
    def troubleshoot_vm(vm: str) -> str:
        return (
            f"Investigate the vSphere virtual machine '{vm}' and explain what is wrong.\n\n"
            "Work through this in order, using the vsphere_* tools:\n"
            f"1. vsphere_get_vm for '{vm}': power state, VMware Tools status, guest IP, "
            "provisioned CPU/memory and the snapshot tree.\n"
            "2. vsphere_get_performance for the VM over the realtime interval. Look at "
            "cpu.usage.average, cpu.ready.summation (CPU contention), mem.usage.average and "
            "mem.swapinRate.average (memory pressure).\n"
            "3. vsphere_get_performance for its host, to separate a noisy VM from a "
            "saturated host.\n"
            "4. vsphere_list_events and vsphere_list_tasks scoped to the VM over the last 24 "
            "hours, for recent changes, HA restarts or failed operations.\n"
            "5. vsphere_list_alarms for triggered alarms on the VM, its host or its datastore.\n"
            "6. vsphere_list_datastores to check whether the datastores it sits on are full.\n\n"
            "Then give: the most likely cause, the evidence for it, and the specific remedial "
            "actions you would take. Call out old snapshots and thin-provisioned datastores "
            "close to full, since both are common and easy to miss."
        )

    @server.prompt(
        name="capacity_report",
        description="Summarise compute and storage capacity, utilisation and risks.",
    )
    def capacity_report(scope: str = "the whole environment") -> str:
        return (
            f"Produce a vSphere capacity report for {scope}.\n\n"
            "Gather data with: vsphere_list_clusters, vsphere_list_hosts, "
            "vsphere_get_vm_summary_by_host, vsphere_list_datastores and vsphere_list_alarms.\n\n"
            "Report on:\n"
            "- CPU and memory utilisation per cluster and per host, and how much headroom "
            "remains if the busiest host fails.\n"
            "- vCPU overcommitment ratios, flagging hosts above 4:1.\n"
            "- Datastores above 80% used, and any datastore whose provisioned space exceeds "
            "its capacity.\n"
            "- Powered-off VMs and templates still consuming storage.\n"
            "- Any triggered alarms that bear on capacity.\n\n"
            "Finish with a prioritised list of recommendations, each with the numbers that "
            "justify it."
        )
