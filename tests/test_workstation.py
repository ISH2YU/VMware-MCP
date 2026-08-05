"""Workstation backend: .vmx parsing, discovery, client and tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from fake_vmrun import FakeVmrun, write_vmx
from vmware_mcp.config import PermissionMode, Product, WorkstationSettings
from vmware_mcp.errors import AmbiguousObjectError, InvalidArgumentError, ObjectNotFoundError
from vmware_mcp.server import create_server
from vmware_mcp.workstation.client import WorkstationClient
from vmware_mcp.workstation.discovery import VmInventory
from vmware_mcp.workstation.vmx import apply_config_changes, guest_os_family, load_vmx


@pytest.fixture
def vm_root(tmp_path: Path) -> Path:
    write_vmx(tmp_path, "win11-golden", guest_os="windows11-64", cpus=4, memory_mb=8192)
    write_vmx(tmp_path, "ubuntu-dev", guest_os="ubuntu-64", cpus=2, memory_mb=2048)
    write_vmx(tmp_path, "win10-legacy", guest_os="windows9-64", cpus=2, memory_mb=4096)
    return tmp_path


@pytest.fixture
def fake(tmp_path: Path) -> FakeVmrun:
    return FakeVmrun(executable_path=tmp_path / "vmrun")


@pytest.fixture
def settings(vm_root: Path) -> WorkstationSettings:
    return WorkstationSettings(
        vm_dirs=(vm_root,),
        product=Product.WORKSTATION,
        permission_mode=PermissionMode.DESTRUCTIVE,
        guest_username="Administrator",
        guest_password="Passw0rd!",
        cache_ttl=0,
    )


@pytest.fixture
def client(settings: WorkstationSettings, fake: FakeVmrun) -> WorkstationClient:
    return WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]


@pytest.fixture
def server(client: WorkstationClient, settings: WorkstationSettings):
    return create_server(settings, client=client)


async def call_ok(server, tool: str, /, **arguments):
    async with Client(server) as session:
        result = await session.call_tool(tool, arguments)
        assert not result.is_error, "\n".join(
            getattr(block, "text", "") for block in result.content
        )
        assert result.structured_content is not None
        return result.structured_content


async def call_tool(server, tool: str, /, **arguments):
    async with Client(server) as session:
        return await session.call_tool(tool, arguments)


# --------------------------------------------------------------------------- #
# .vmx
# --------------------------------------------------------------------------- #


def test_load_vmx_reads_the_fields_that_matter(vm_root: Path):
    vmx = load_vmx(vm_root / "win11-golden" / "win11-golden.vmx")
    summary = vmx.summary()
    assert summary["name"] == "win11-golden"
    assert summary["guest_os"] == "windows11-64"
    assert summary["guest_os_family"] == "windows"
    assert summary["cpu_count"] == 4
    assert summary["memory_mb"] == 8192
    assert summary["memory_gib"] == 8.0
    assert summary["firmware"] == "efi"
    assert summary["ethernet"][0]["connection_type"] == "nat"
    assert summary["disks"][0]["file"] == "win11-golden.vmdk"


def test_guest_os_family_detection():
    assert guest_os_family("windows11-64") == "windows"
    assert guest_os_family("ubuntu-64") == "linux"
    assert guest_os_family("darwin22-64") == "macos"
    assert guest_os_family(None) is None


def test_apply_config_changes_updates_and_rewrites(vm_root: Path):
    path = vm_root / "ubuntu-dev" / "ubuntu-dev.vmx"
    vmx = load_vmx(path)
    changes = apply_config_changes(vmx, name="ubuntu-ci", cpu_count=8, memory_mb=16384)
    vmx.write()
    reloaded = load_vmx(path)
    assert reloaded.get("displayname") == "ubuntu-ci"
    assert reloaded.get("numvcpus") == "8"
    assert reloaded.get("memsize") == "16384"
    assert changes["previous"]["cpu_count"] == 2
    assert changes["current"]["cpu_count"] == 8


def test_apply_config_changes_validates(vm_root: Path):
    vmx = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")
    with pytest.raises(InvalidArgumentError):
        apply_config_changes(vmx, cpu_count=0)
    with pytest.raises(InvalidArgumentError):
        apply_config_changes(vmx, memory_mb=1)
    with pytest.raises(InvalidArgumentError):
        apply_config_changes(vmx, cpu_count=6, cores_per_socket=4)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_inventory_discovers_all_vms(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    names = [vm.name for vm in inventory.list()]
    assert names == ["ubuntu-dev", "win10-legacy", "win11-golden"]


def test_resolve_by_name_path_stem_and_uuid(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    by_name = inventory.resolve("win11-golden")
    by_path = inventory.resolve(str(by_name.path))
    by_stem = inventory.resolve("win11-golden")
    assert by_name.path == by_path.path == by_stem.path
    assert inventory.resolve(by_name.uuid or "").path == by_name.path


def test_ambiguous_names_are_reported(vm_root: Path):
    write_vmx(vm_root / "other", "win11-golden", guest_os="windows11-64")
    # Same displayName in a nested folder — force two with same name via rewrite
    inventory = VmInventory((vm_root,), ttl=0)
    # Two VMs named win11-golden
    with pytest.raises(AmbiguousObjectError) as excinfo:
        inventory.resolve("win11-golden")
    assert "2 VMs match" in str(excinfo.value)


def test_unknown_vm_lists_what_exists(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(ObjectNotFoundError) as excinfo:
        inventory.resolve("nope")
    assert "win11-golden" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


async def test_list_vms_filters(client: WorkstationClient, fake: FakeVmrun, vm_root: Path):
    golden = str(vm_root / "win11-golden" / "win11-golden.vmx")
    fake.running.add(golden)
    windows = await client.list_vms(guest_os_family="windows")
    assert {vm["name"] for vm in windows} == {"win11-golden", "win10-legacy"}
    running = await client.list_vms(running_only=True)
    assert [vm["name"] for vm in running] == ["win11-golden"]
    assert running[0]["power_state"] == "poweredOn"


async def test_power_start_and_stop(client: WorkstationClient, fake: FakeVmrun):
    started = await client.change_power("win11-golden", "start")
    assert started["status"] == "completed"
    assert fake.methods("start")
    again = await client.change_power("win11-golden", "start")
    assert again["status"] == "no_change"
    stopped = await client.change_power("win11-golden", "stop")
    assert stopped["mode"] == "soft"


async def test_clone_linked_renames_the_display_name(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path, tmp_path: Path
):
    dest = tmp_path / "clones" / "win11-test-01"
    result = await client.clone_vm(
        "win11-golden",
        "win11-test-01",
        destination_dir=str(dest),
        clone_type="linked",
        snapshot="golden",
    )
    assert result["status"] == "completed"
    assert Path(result["path"]).is_file()
    assert load_vmx(result["path"]).get("displayname") == "win11-test-01"
    call = fake.methods("clone")[0]
    assert call.args[2] == "linked"
    assert call.args[3] == "golden"


async def test_clone_many_continues_after_a_failure(
    client: WorkstationClient, fake: FakeVmrun, tmp_path: Path
):
    # Make the second clone fail by pre-creating its destination .vmx
    dest_root = tmp_path / "batch"
    pre = dest_root / "lab-02"
    pre.mkdir(parents=True)
    (pre / "lab-02.vmx").write_text('.encoding = "UTF-8"\ndisplayName = "lab-02"\n')

    result = await client.clone_many(
        "win11-golden",
        3,
        name_prefix="lab",
        destination_dir=str(dest_root),
        clone_type="linked",
    )
    assert result["created"] == 2
    assert result["failed"] == 1
    assert result["errors"][0]["name"] == "lab-02"


async def test_reconfigure_requires_power_off(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    fake.running.add(str(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx"))
    with pytest.raises(InvalidArgumentError, match="powered on"):
        await client.reconfigure_vm("ubuntu-dev", cpu_count=8)


async def test_reconfigure_when_powered_off(client: WorkstationClient):
    result = await client.reconfigure_vm("ubuntu-dev", cpu_count=8, memory_mb=8192)
    assert result["current"]["cpu_count"] == 8
    assert result["current"]["memory_mb"] == 8192


async def test_delete_requires_confirm(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="confirm=true"):
        await client.delete_vm("ubuntu-dev", confirm=False)


async def test_snapshots(client: WorkstationClient, fake: FakeVmrun, vm_root: Path):
    path = str(vm_root / "win11-golden" / "win11-golden.vmx")
    await client.create_snapshot("win11-golden", "golden")
    assert fake.snapshots[path] == ["golden"]
    listed = await client.list_snapshots("win11-golden")
    assert listed == [{"name": "golden", "path": "golden"}]
    await client.revert_snapshot("win11-golden", "golden")
    await client.delete_snapshot("win11-golden", "golden")
    assert fake.snapshots[path] == []


async def test_guest_run_command_captures_output(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    path = str(vm_root / "win11-golden" / "win11-golden.vmx")
    fake.running.add(path)
    fake.tools_state[path] = "running"
    fake.ips[path] = "192.168.1.50"
    auth = client.auth()
    result = await client.guest.run_program(
        Path(path),
        "cmd.exe",
        "/C echo hi",
        auth=auth,
        guest_os="windows11-64",
    )
    assert result.exit_code == 0
    assert "hello from guest" in result.stdout


async def test_wait_for_guest(client: WorkstationClient, fake: FakeVmrun, vm_root: Path):
    path = str(vm_root / "win11-golden" / "win11-golden.vmx")
    fake.tools_state[path] = "running"
    fake.ips[path] = "10.0.0.5"
    tools = await client.guest.wait_for_tools(Path(path), timeout=2)
    ip = await client.guest.wait_for_ip(Path(path), timeout=2)
    assert tools == "running"
    assert ip == "10.0.0.5"


# --------------------------------------------------------------------------- #
# Tools over MCP
# --------------------------------------------------------------------------- #


async def test_about_tool(server):
    about = await call_ok(server, "vmware_about")
    assert about["backend"] == "workstation"
    assert about["vm_count"] == 3
    assert about["guest_credentials_configured"] is True
    assert about["connection"]["permission_mode"] == "destructive"


async def test_list_and_get_vm_tools(server):
    listed = await call_ok(server, "vmware_list_vms", guest_os_family="windows")
    assert listed["total_matched"] == 2
    detail = await call_ok(server, "vmware_get_vm", vm="win11-golden")
    assert detail["vm"]["name"] == "win11-golden"
    assert detail["vm"]["guest_os_family"] == "windows"


async def test_clone_many_tool(server, fake: FakeVmrun, tmp_path: Path):
    result = await call_ok(
        server,
        "vmware_clone_many",
        vm="win11-golden",
        count=2,
        name_prefix="test",
        destination_dir=str(tmp_path / "out"),
        snapshot="golden",
    )
    assert result["created"] == 2
    assert [vm["name"] for vm in result["vms"]] == ["test-01", "test-02"]


async def test_power_tool(server, fake: FakeVmrun):
    result = await call_ok(server, "vmware_power_vm", vm="ubuntu-dev", action="start")
    assert result["status"] == "completed"
    assert fake.methods("start")


async def test_read_only_mode_blocks_cloning(vm_root: Path, fake: FakeVmrun):
    settings = WorkstationSettings(
        vm_dirs=(vm_root,),
        product=Product.WORKSTATION,
        permission_mode=PermissionMode.READ_ONLY,
        cache_ttl=0,
    )
    client = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
    server = create_server(settings, client=client)
    result = await call_tool(server, "vmware_clone_vm", vm="win11-golden", name="x")
    assert result.is_error
    text = "\n".join(getattr(block, "text", "") for block in result.content)
    assert "read-only" in text


async def test_delete_tool_requires_confirm(server):
    result = await call_tool(server, "vmware_delete_vm", vm="ubuntu-dev", confirm=False)
    assert result.is_error


async def test_server_advertises_workstation_prompts(server):
    async with Client(server) as session:
        prompts = {prompt.name for prompt in (await session.list_prompts()).prompts}
    assert prompts == {"spin_up_test_vms", "run_windows_test", "reset_test_vms"}


async def test_twenty_workstation_tools_are_registered(server):
    async with Client(server) as session:
        tools = (await session.list_tools()).tools
    assert len(tools) == 20
    assert all(tool.name.startswith("vmware_") for tool in tools)
