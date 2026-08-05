"""Assembly of the VMware MCP server."""

from __future__ import annotations

import json
import logging

from mcp.server import MCPServer

from .config import PermissionMode, Settings, load_settings
from .tools import ToolContext, register_all
from .workstation import WorkstationClient

logger = logging.getLogger(__name__)

SERVER_NAME = "vmware-mcp"


def build_instructions(settings: Settings) -> str:
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

    dirs = ", ".join(str(path) for path in settings.vm_dirs) or "(none configured)"
    guest = (
        f"Guest credentials are configured for user {settings.guest_username!r}."
        if settings.has_guest_credentials
        else "No guest credentials configured — set VMWARE_GUEST_USERNAME / "
        "VMWARE_GUEST_PASSWORD to run commands inside VMs."
    )
    return f"""Tools for local VMware {settings.product.value} VMs on this machine.

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


def create_server(
    settings: Settings | None = None, client: WorkstationClient | None = None
) -> MCPServer:
    """Build the MCP server, its tools, resources and prompts."""
    resolved = settings or load_settings()
    ws = client or WorkstationClient(resolved)
    context = ToolContext(client=ws, settings=resolved)

    server = MCPServer(
        name=SERVER_NAME,
        title="VMware Workstation",
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


def _register_prompts(server: MCPServer) -> None:
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
            f"1. vmware_list_vms with name='{name_prefix}*'.\n"
            "2. For each match: stop it if running, then vmware_revert_snapshot.\n"
            "3. Summarise what was reverted and anything that failed.\n"
        )


__all__ = ["build_instructions", "create_server"]
