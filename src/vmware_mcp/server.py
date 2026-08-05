"""Assembly of the VMware MCP server."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import MCPServer

from .config import (
    Backend,
    BaseSettings,
    PermissionMode,
    VSphereSettings,
    WorkstationSettings,
    load_settings,
)
from .tools import ToolContext, register_all

logger = logging.getLogger(__name__)

SERVER_NAME = "vmware-mcp"


def build_instructions(settings: BaseSettings) -> str:
    mode = settings.permission_mode
    if mode is PermissionMode.READ_ONLY:
        capability = (
            "This server is READ-ONLY: listing and inspecting VMs works, and every "
            "tool that would change anything will refuse."
        )
    elif mode is PermissionMode.WRITE:
        capability = (
            "This server may change VMs: power operations, snapshot creation, cloning, "
            "reconfiguration and guest commands are allowed. Deleting VMs and reverting "
            "or deleting snapshots is not."
        )
    else:
        capability = (
            "This server has FULL access, including deleting VMs and reverting or "
            "deleting snapshots. Confirm destructive actions with the user before "
            "running them."
        )

    if isinstance(settings, WorkstationSettings):
        dirs = ", ".join(str(path) for path in settings.vm_dirs) or "(none configured)"
        guest = (
            f"Guest credentials are configured for user {settings.guest_username!r}."
            if settings.has_guest_credentials
            else "No guest credentials configured — set VMWARE_GUEST_USERNAME / "
            "VMWARE_GUEST_PASSWORD to run commands inside VMs."
        )
        return f"""Tools for local VMware {settings.product.value} VMs.

{capability}

VM directories scanned: {dirs}
{guest}

Identifying VMs: every tool accepts the display name, the full .vmx path, the
directory name, or the BIOS UUID. Names are matched case-insensitively; if
several VMs share a name the tool lists the candidates with their paths rather
than guessing.

Typical Windows test-lab workflow:
1. vmware_about — confirm product, permission mode and guest credentials.
2. vmware_list_vms / vmware_get_vm — find the golden/template VM.
3. vmware_create_snapshot on the golden image if it does not already have one.
4. vmware_clone_many from that snapshot (linked clones) to spin up N test VMs.
5. vmware_power_vm to start them (gui=false for headless).
6. vmware_wait_for_guest until Tools is running and an IP is assigned.
7. vmware_copy_to_guest / vmware_run_command / vmware_run_script to install and test.
8. vmware_revert_snapshot or vmware_delete_vm when finished.

Listings are paginated. When a result has truncated: true there is more to fetch
with offset.
"""

    assert isinstance(settings, VSphereSettings)
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
  fetch with `offset`.
- Long operations (clone, migrate, snapshot) return a `task_id`. Pass
  `wait=false` for slow work and poll with `vsphere_get_task`.
"""


def create_server(settings: BaseSettings | None = None, client: Any | None = None) -> MCPServer:
    """Build the MCP server for the configured backend.

    ``client`` exists so tests can supply a double; production callers pass
    settings only (or nothing, and settings are loaded from the environment).
    """
    resolved = settings or load_settings()
    backend_client = client or _build_client(resolved)
    context = ToolContext(client=backend_client, settings=resolved)

    title = "VMware Workstation" if isinstance(resolved, WorkstationSettings) else "VMware vSphere"
    server = MCPServer(
        name=SERVER_NAME,
        title=title,
        version="0.1.0",
        instructions=build_instructions(resolved),
        website_url="https://github.com/ISH2YU/VMware-MCP",
    )

    register_all(server, context)
    if isinstance(resolved, WorkstationSettings):
        _register_workstation_resources(server, context)
        _register_workstation_prompts(server)
    else:
        _register_vsphere_resources(server, context)
        _register_vsphere_prompts(server)
    return server


def _build_client(settings: BaseSettings) -> Any:
    if isinstance(settings, WorkstationSettings):
        from .workstation import WorkstationClient

        return WorkstationClient(settings)
    from .vsphere.client import VSphereClient

    assert isinstance(settings, VSphereSettings)
    return VSphereClient(settings)


def _register_workstation_resources(server: MCPServer, context: ToolContext) -> None:
    client = context.client

    @server.resource(
        "vmware://vms",
        name="Local virtual machines",
        description="Every local VM discovered under the configured directories.",
        mime_type="application/json",
    )
    async def list_vms_resource() -> str:
        vms = await client.list_vms()
        return json.dumps({"count": len(vms), "vms": vms}, indent=2)

    @server.resource(
        "vmware://vm/{identifier}",
        name="Local virtual machine detail",
        description="Full configuration and runtime detail for one local VM.",
        mime_type="application/json",
    )
    async def vm_detail(identifier: str) -> str:
        return json.dumps(await client.get_vm(identifier), indent=2, default=str)


def _register_workstation_prompts(server: MCPServer) -> None:
    @server.prompt(
        name="spin_up_test_vms",
        description="Clone N disposable test VMs from a golden image and prepare them.",
    )
    def spin_up_test_vms(
        template: str, count: str = "3", name_prefix: str = "test", snapshot: str = "golden"
    ) -> str:
        return (
            f"Spin up {count} disposable test VMs from the golden image '{template}'.\n\n"
            "Do this, using the vmware_* tools:\n"
            f"1. vmware_get_vm on '{template}' to confirm it exists and note its snapshots.\n"
            f"2. If there is no snapshot named '{snapshot}', create one with "
            f"vmware_create_snapshot.\n"
            f"3. vmware_clone_many from '{template}' with count={count}, "
            f"name_prefix='{name_prefix}', snapshot='{snapshot}', clone_type='linked', "
            f"start=true.\n"
            "4. For each clone, vmware_wait_for_guest until Tools is running and an IP "
            "is assigned.\n"
            "5. Report the resulting VM names, paths and IP addresses.\n\n"
            "If anything fails, say what failed and which VMs were created successfully."
        )

    @server.prompt(
        name="run_windows_test",
        description="Copy a package into a Windows VM, install it quietly, and report the result.",
    )
    def run_windows_test(
        vm: str, installer_host_path: str, guest_path: str = r"C:\Temp\setup.exe"
    ) -> str:
        return (
            f"Run a Windows install test on '{vm}'.\n\n"
            "Steps:\n"
            f"1. vmware_get_vm to confirm '{vm}' is powered on; start it if not.\n"
            "2. vmware_wait_for_guest until Tools and an IP are ready.\n"
            f"3. vmware_run_command to ensure C:\\Temp exists "
            f"(cmd.exe /C mkdir C:\\Temp).\n"
            f"4. vmware_copy_to_guest from '{installer_host_path}' to '{guest_path}'.\n"
            f"5. vmware_run_command to install quietly, e.g. "
            f'cmd.exe /C "{guest_path} /quiet /norestart" '
            f"(adjust silent flags to the installer).\n"
            "6. Report the exit code, stdout/stderr, and take vmware_screenshot if the "
            "exit code is non-zero.\n"
        )

    @server.prompt(
        name="reset_test_vms",
        description="Revert a set of test VMs back to a clean snapshot.",
    )
    def reset_test_vms(name_prefix: str, snapshot: str = "golden") -> str:
        return (
            f"Reset every local VM whose name starts with '{name_prefix}' back to "
            f"snapshot '{snapshot}'.\n\n"
            "1. vmware_list_vms with name='{name_prefix}*'.\n"
            "2. For each match: stop it if running, then vmware_revert_snapshot.\n"
            "3. Summarise what was reverted and anything that failed.\n"
        )


def _register_vsphere_resources(server: MCPServer, context: ToolContext) -> None:
    from pyVmomi import vim

    from .vsphere import lookup, mappers

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
            "endpoint": context.settings.endpoint,  # type: ignore[attr-defined]
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


def _register_vsphere_prompts(server: MCPServer) -> None:
    @server.prompt(
        name="troubleshoot_vm",
        description="Investigate why a virtual machine is slow, stuck or unreachable.",
    )
    def troubleshoot_vm(vm: str) -> str:
        return (
            f"Investigate the vSphere virtual machine '{vm}' and explain what is wrong.\n\n"
            "Work through this in order, using the vsphere_* tools:\n"
            f"1. vsphere_get_vm for '{vm}'.\n"
            "2. vsphere_get_performance for the VM and its host.\n"
            "3. vsphere_list_events and vsphere_list_tasks scoped to the VM.\n"
            "4. vsphere_list_alarms and vsphere_list_datastores.\n"
            "Then give the most likely cause, the evidence, and remedial actions."
        )

    @server.prompt(
        name="capacity_report",
        description="Summarise compute and storage capacity, utilisation and risks.",
    )
    def capacity_report(scope: str = "the whole environment") -> str:
        return (
            f"Produce a vSphere capacity report for {scope}.\n\n"
            "Gather data with vsphere_list_clusters, vsphere_list_hosts, "
            "vsphere_get_vm_summary_by_host, vsphere_list_datastores and "
            "vsphere_list_alarms. Finish with prioritised recommendations."
        )


# Re-export for callers that branch on backend.
__all__ = ["Backend", "build_instructions", "create_server"]
