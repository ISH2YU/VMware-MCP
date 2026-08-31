"""The tools as an MCP client sees them: schemas, permissions and error text."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from conftest import make_client
from fake_vmrun import FakeVmrun, write_vmx
from mcp_client import call_error, call_ok, call_tool
from vmware_mcp.config import PermissionMode
from vmware_mcp.server import create_server

EXPECTED_TOOLS = {
    "vmware_about",
    "vmware_clone_many",
    "vmware_clone_vm",
    "vmware_copy_from_guest",
    "vmware_copy_to_guest",
    "vmware_create_snapshot",
    "vmware_delete_many",
    "vmware_delete_snapshot",
    "vmware_delete_vm",
    "vmware_get_vm",
    "vmware_list_guest_directory",
    "vmware_list_running",
    "vmware_list_snapshots",
    "vmware_list_vms",
    "vmware_power_many",
    "vmware_power_vm",
    "vmware_reconfigure_vm",
    "vmware_revert_many",
    "vmware_revert_snapshot",
    "vmware_run_command",
    "vmware_run_script",
    "vmware_screenshot",
    "vmware_wait_for_guest",
}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


async def test_the_expected_tools_are_registered(server):
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == EXPECTED_TOOLS


async def test_every_tool_is_documented_and_annotated(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert len(tool.description) > 40, f"{tool.name} description is too thin"


async def test_read_and_write_hints_are_honest(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in ("vmware_about", "vmware_list_vms", "vmware_get_vm", "vmware_list_running"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("vmware_clone_many", "vmware_run_command", "vmware_reconfigure_vm"):
        assert tools[name].annotations.read_only_hint is False, name
    for name in ("vmware_delete_vm", "vmware_delete_many", "vmware_revert_many"):
        assert tools[name].annotations.destructive_hint is True, name


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


async def test_about(server):
    about = await call_ok(server, "vmware_about")
    assert about["product"] == "ws"
    assert about["vm_count"] == 3
    assert about["configuration"]["permission_mode"] == "destructive"


async def test_list_and_get(server):
    listed = await call_ok(server, "vmware_list_vms", guest_os_family="windows")
    assert listed["total_matched"] == 2
    detail = await call_ok(server, "vmware_get_vm", vm="win11-golden")
    assert detail["vm"]["name"] == "win11-golden"


async def test_pagination_reports_truncation(server):
    page = await call_ok(server, "vmware_list_vms", limit=2)
    assert page["returned"] == 2
    assert page["total_matched"] == 3
    assert page["truncated"] is True
    rest = await call_ok(server, "vmware_list_vms", limit=2, offset=2)
    assert rest["returned"] == 1
    assert rest["truncated"] is False


async def test_pagination_rejects_bad_input(server):
    assert "offset" in await call_error(server, "vmware_list_vms", offset=-1)
    assert "limit" in await call_error(server, "vmware_list_vms", limit=0)


async def test_power_tool(server, fake: FakeVmrun):
    result = await call_ok(server, "vmware_power_vm", vm="ubuntu-dev", action="start")
    assert result["status"] == "completed"
    assert fake.methods("start")


async def test_clone_many_tool(server, vm_root: Path):
    result = await call_ok(
        server,
        "vmware_clone_many",
        vm="win11-golden",
        count=2,
        name_prefix="test",
        destination_dir=str(vm_root / "out"),
        snapshot="golden",
    )
    assert result["created"] == 2
    assert [vm["name"] for vm in result["vms"]] == ["test-01", "test-02"]


async def test_wait_for_guest_tool(server, fake: FakeVmrun, golden: str):
    fake.tools_state[golden] = "running"
    fake.ips[golden] = "10.0.0.8"
    result = await call_ok(server, "vmware_wait_for_guest", vm="win11-golden")
    assert result["tools_state"] == "running"
    assert result["ip_address"] == "10.0.0.8"


async def test_run_command_tool_marks_output_untrusted(server):
    result = await call_ok(
        server, "vmware_run_command", vm="win11-golden", program="cmd.exe", arguments="/C dir"
    )
    assert result["exit_code"] == 0
    assert "hello from guest" in result["stdout"]
    assert result["output_is_untrusted"] is True


async def test_run_script_tool(server):
    result = await call_ok(server, "vmware_run_script", vm="win11-golden", script="Get-Date\n")
    assert result["exit_code"] == 0


async def test_snapshot_tools(server):
    await call_ok(server, "vmware_create_snapshot", vm="win11-golden", name="golden")
    listed = await call_ok(server, "vmware_list_snapshots", vm="win11-golden")
    assert listed["count"] == 1
    await call_ok(server, "vmware_revert_snapshot", vm="win11-golden", snapshot="golden")
    await call_ok(server, "vmware_delete_snapshot", vm="win11-golden", snapshot="golden")


async def test_guest_file_tools(server, fake: FakeVmrun, vm_root: Path, golden: str):
    installer = vm_root / "app.msi"
    installer.write_bytes(b"MSI")
    pushed = await call_ok(
        server,
        "vmware_copy_to_guest",
        vm="win11-golden",
        host_path=str(installer),
        guest_path=r"C:\Temp\app.msi",
    )
    assert pushed["bytes"] == 3
    listed = await call_ok(
        server, "vmware_list_guest_directory", vm="win11-golden", path=r"C:\Temp"
    )
    assert "app.msi" in listed["entries"]
    pulled = await call_ok(
        server,
        "vmware_copy_from_guest",
        vm="win11-golden",
        guest_path=r"C:\Temp\app.msi",
        host_path=str(vm_root / "back" / "app.msi"),
    )
    assert Path(pulled["host_path"]).read_bytes() == b"MSI"


async def test_screenshot_tool(server):
    result = await call_ok(server, "vmware_screenshot", vm="win11-golden")
    assert Path(result["screenshot"]).is_file()


async def test_reconfigure_tool(server):
    result = await call_ok(server, "vmware_reconfigure_vm", vm="ubuntu-dev", cpu_count=8)
    assert result["current"]["cpu_count"] == 8


# --------------------------------------------------------------------------- #
# Batch tools
# --------------------------------------------------------------------------- #


@pytest.fixture
def lab(vm_root: Path) -> Path:
    for index in (1, 2):
        write_vmx(vm_root, f"web-test-0{index}", guest_os="windows11-64")
    return vm_root


async def test_power_many_tool(lab: Path, server, fake: FakeVmrun):
    result = await call_ok(server, "vmware_power_many", pattern="web-test-*", action="start")
    assert result["succeeded"] == 2
    assert len(fake.methods("start")) == 2


async def test_revert_many_tool(lab: Path, server, fake: FakeVmrun):
    for index in (1, 2):
        path = str(lab / f"web-test-0{index}" / f"web-test-0{index}.vmx")
        fake.snapshots[path] = ["golden"]
    result = await call_ok(server, "vmware_revert_many", pattern="web-test-*", snapshot="golden")
    assert result["succeeded"] == 2


async def test_delete_many_dry_run_lists_matches(lab: Path, server, fake: FakeVmrun):
    result = await call_ok(server, "vmware_delete_many", pattern="web-test-*", dry_run=True)
    assert result["matched"] == 2
    assert result["dry_run"] is True
    assert not fake.methods("deleteVM")


async def test_delete_many_needs_confirmation(lab: Path, server):
    assert "confirm=true" in await call_error(server, "vmware_delete_many", pattern="web-test-*")


async def test_delete_many_tool(lab: Path, server):
    result = await call_ok(server, "vmware_delete_many", pattern="web-test-*", confirm=True)
    assert result["succeeded"] == 2


# --------------------------------------------------------------------------- #
# Permission modes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("vmware_power_vm", {"vm": "win11-golden", "action": "start"}),
        ("vmware_clone_vm", {"vm": "win11-golden", "name": "x"}),
        ("vmware_clone_many", {"vm": "win11-golden", "count": 1, "name_prefix": "x"}),
        ("vmware_create_snapshot", {"vm": "win11-golden", "name": "s"}),
        ("vmware_reconfigure_vm", {"vm": "win11-golden", "cpu_count": 2}),
        ("vmware_screenshot", {"vm": "win11-golden"}),
        ("vmware_run_command", {"vm": "win11-golden", "program": "cmd.exe"}),
        ("vmware_run_script", {"vm": "win11-golden", "script": "x"}),
        (
            "vmware_copy_to_guest",
            {"vm": "win11-golden", "host_path": "/tmp/x", "guest_path": "C:/x"},
        ),
        (
            "vmware_copy_from_guest",
            {"vm": "win11-golden", "guest_path": "C:/x", "host_path": "/tmp/x"},
        ),
        ("vmware_power_many", {"pattern": "win*", "action": "start"}),
    ],
)
async def test_read_only_mode_blocks_every_mutating_tool(
    vm_root: Path, fake: FakeVmrun, tool: str, arguments: dict
):
    client = make_client(vm_root, fake, mode=PermissionMode.READ_ONLY)
    server = create_server(client.settings, client=client)
    message = await call_error(server, tool, **arguments)
    assert "read-only" in message
    assert "VMWARE_PERMISSION_MODE" in message


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("vmware_revert_snapshot", {"vm": "win11-golden", "snapshot": "s"}),
        ("vmware_delete_snapshot", {"vm": "win11-golden", "snapshot": "s"}),
        ("vmware_delete_vm", {"vm": "win11-golden", "confirm": True}),
        ("vmware_delete_many", {"pattern": "win*", "confirm": True}),
        ("vmware_revert_many", {"pattern": "win*", "snapshot": "s"}),
    ],
)
async def test_write_mode_still_blocks_destructive_tools(
    vm_root: Path, fake: FakeVmrun, tool: str, arguments: dict
):
    client = make_client(vm_root, fake, mode=PermissionMode.WRITE)
    server = create_server(client.settings, client=client)
    message = await call_error(server, tool, **arguments)
    assert "destructive" in message


async def test_read_only_mode_still_allows_inspection(vm_root: Path, fake: FakeVmrun):
    client = make_client(vm_root, fake, mode=PermissionMode.READ_ONLY)
    server = create_server(client.settings, client=client)
    assert (await call_ok(server, "vmware_list_vms"))["total_matched"] == 3
    assert (await call_ok(server, "vmware_about"))["vm_count"] == 3


async def test_dry_runs_are_allowed_in_read_only_mode(vm_root: Path, fake: FakeVmrun):
    client = make_client(vm_root, fake, mode=PermissionMode.READ_ONLY)
    server = create_server(client.settings, client=client)
    result = await call_ok(server, "vmware_delete_many", pattern="win*", dry_run=True)
    assert result["matched"] == 2
    assert not fake.methods("deleteVM")


# --------------------------------------------------------------------------- #
# Error reporting
# --------------------------------------------------------------------------- #


async def test_unknown_vm_error_reaches_the_client(server):
    message = await call_error(server, "vmware_get_vm", vm="does-not-exist")
    assert "No VM matches" in message
    assert "win11-golden" in message, "the message should list what does exist"


async def test_delete_without_confirm_explains_itself(server):
    assert "confirm=true" in await call_error(
        server, "vmware_delete_vm", vm="ubuntu-dev", confirm=False
    )


async def test_out_of_library_path_is_refused_through_the_tool(server, tmp_path: Path):
    sneaky = write_vmx(tmp_path / "elsewhere", "sneaky")
    message = await call_error(server, "vmware_get_vm", vm=str(sneaky))
    assert "outside" in message
    assert "VMWARE_VM_DIRS" in message


async def test_vmrun_failures_surface_their_own_message(server, fake: FakeVmrun):
    fake.fail_commands.add("snapshot")
    message = await call_error(server, "vmware_create_snapshot", vm="win11-golden", name="boom")
    assert "simulated failure" in message


async def test_a_soft_failure_on_stdout_is_still_a_failure(server, fake: FakeVmrun):
    fake.soft_fail_commands.add("snapshot")
    message = await call_error(server, "vmware_create_snapshot", vm="win11-golden", name="boom")
    assert "soft failure" in message


async def test_errors_do_not_leak_the_guest_password(server, fake: FakeVmrun):
    fake.fail_commands.add("runProgramInGuest")
    result = await call_tool(server, "vmware_run_command", vm="win11-golden", program="cmd.exe")
    assert result.is_error
    assert "Passw0rd!" not in str(result.content)
