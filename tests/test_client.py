"""Power, snapshots, cloning, deletion and batch fan-out."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_client
from fake_vmrun import FakeVmrun, write_vmx
from vmware_mcp.errors import InvalidArgumentError, ObjectNotFoundError, VmrunError
from vmware_mcp.workstation.client import WorkstationClient
from vmware_mcp.workstation.vmx import load_vmx

# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


async def test_about_summarises_the_environment(client: WorkstationClient):
    about = await client.about()
    assert about["product"] == "ws"
    assert about["vm_count"] == 3
    assert about["running_count"] == 0
    assert about["guest_credentials_configured"] is True
    assert about["permission_mode"] == "destructive"
    assert "vmrun version" in (about["vmrun_version"] or "")


async def test_about_never_leaks_the_password(client: WorkstationClient):
    assert "Passw0rd!" not in repr(await client.about())


async def test_list_vms_filters_by_family_and_power(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    fake.running.add(golden)
    windows = await client.list_vms(guest_os_family="windows")
    assert {vm["name"] for vm in windows} == {"win11-golden", "win10-legacy"}
    running = await client.list_vms(running_only=True)
    assert [vm["name"] for vm in running] == ["win11-golden"]
    assert running[0]["power_state"] == "poweredOn"
    off = await client.list_vms(powered_off_only=True)
    assert {vm["name"] for vm in off} == {"ubuntu-dev", "win10-legacy"}


async def test_contradictory_power_filters_are_rejected(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="cannot both be true"):
        await client.list_vms(running_only=True, powered_off_only=True)


async def test_list_vms_supports_globs(client: WorkstationClient):
    assert {vm["name"] for vm in await client.list_vms(name="win*")} == {
        "win11-golden",
        "win10-legacy",
    }
    assert [vm["name"] for vm in await client.list_vms(name="ubuntu")] == ["ubuntu-dev"]


async def test_get_vm_includes_hardware_and_snapshots(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    await client.create_snapshot("win11-golden", "golden")
    fake.running.add(golden)
    fake.tools_state[golden] = "running"
    fake.ips[golden] = "192.168.1.50"
    detail = await client.get_vm("win11-golden")
    assert detail["cpu_count"] == 4
    assert detail["memory_mb"] == 8192
    assert detail["firmware"] == "efi"
    assert detail["power_state"] == "poweredOn"
    assert detail["tools_state"] == "running"
    assert detail["ip_address"] == "192.168.1.50"
    assert detail["snapshots"] == [{"name": "golden"}]
    assert detail["ethernet"][0]["connection_type"] == "nat"
    assert detail["disks"][0]["file"] == "win11-golden.vmdk"


async def test_a_powered_off_vm_reports_no_tools_or_ip(client: WorkstationClient):
    detail = await client.get_vm("ubuntu-dev")
    assert detail["power_state"] == "poweredOff"
    assert detail["tools_state"] == "unknown"
    assert detail["ip_address"] is None


async def test_running_vms_outside_the_library_are_hidden(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    fake.running.add(golden)
    fake.running.add("/somewhere/else/private.vmx")
    listed = await client.list_running()
    assert golden in listed
    assert not any("private.vmx" in item for item in listed)


async def test_find_vms_returns_sorted_matches(client: WorkstationClient):
    assert [vm.name for vm in await client.find_vms("win*")] == ["win10-legacy", "win11-golden"]


async def test_the_sandbox_fails_closed_without_configured_directories(
    fake: FakeVmrun, golden: str
):
    """With no VM library configured nothing is visible, rather than everything."""
    from vmware_mcp.config import PermissionMode, Product, Settings

    settings = Settings(
        vm_dirs=(),
        product=Product.WORKSTATION,
        permission_mode=PermissionMode.DESTRUCTIVE,
    )
    client = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
    fake.running.add(golden)
    fake.running.add("/anywhere/else/private.vmx")
    assert await client.list_running() == []
    assert await client.list_vms() == []


async def test_cloning_without_configured_directories_is_refused(fake: FakeVmrun, vm_root: Path):
    from vmware_mcp.config import PermissionMode, Product, Settings

    inner = make_client(vm_root, fake)
    source = await inner.resolve_async("win11-golden")
    settings = Settings(
        vm_dirs=(), product=Product.WORKSTATION, permission_mode=PermissionMode.DESTRUCTIVE
    )
    client = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError, match="No VM directories are configured"):
        await client._clone_resolved(
            source, "orphan", destination_dir=None, clone_type="linked", snapshot=None
        )


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


async def test_start_then_stop(client: WorkstationClient, fake: FakeVmrun):
    started = await client.change_power("win11-golden", "start")
    assert started["status"] == "completed"
    assert started["mode"] == "nogui"
    assert fake.methods("start")[0].args[1] == "nogui"

    stopped = await client.change_power("win11-golden", "stop")
    assert stopped["status"] == "completed"
    assert stopped["mode"] == "soft"


async def test_gui_start_asks_for_a_window(client: WorkstationClient, fake: FakeVmrun):
    await client.change_power("win11-golden", "start", gui=True)
    assert fake.methods("start")[0].args[1] == "gui"


async def test_starting_a_running_vm_is_a_no_op(client: WorkstationClient):
    await client.change_power("win11-golden", "start")
    again = await client.change_power("win11-golden", "start")
    assert again["status"] == "no_change"
    assert again["power_state"] == "poweredOn"


async def test_stopping_a_stopped_vm_is_a_no_op(client: WorkstationClient):
    result = await client.change_power("win11-golden", "stop")
    assert result["status"] == "no_change"


async def test_hard_stop_uses_hard_mode(client: WorkstationClient, fake: FakeVmrun):
    await client.change_power("win11-golden", "start")
    result = await client.change_power("win11-golden", "hard_stop")
    assert result["mode"] == "hard"
    assert fake.methods("stop")[0].args[1] == "hard"


async def test_reset_requires_a_running_vm(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="not running"):
        await client.change_power("win11-golden", "reset")


async def test_reset_modes(client: WorkstationClient, fake: FakeVmrun):
    await client.change_power("win11-golden", "start")
    assert (await client.change_power("win11-golden", "reset"))["mode"] == "soft"
    assert (await client.change_power("win11-golden", "hard_reset"))["mode"] == "hard"


async def test_suspend_and_pause(client: WorkstationClient):
    assert (await client.change_power("win11-golden", "suspend"))["status"] == "no_change"
    await client.change_power("win11-golden", "start")
    assert (await client.change_power("win11-golden", "suspend"))["status"] == "completed"
    await client.change_power("win11-golden", "start")
    assert (await client.change_power("win11-golden", "pause"))["status"] == "completed"
    assert (await client.change_power("win11-golden", "unpause"))["status"] == "completed"


async def test_pausing_a_stopped_vm_fails(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="not running"):
        await client.change_power("win11-golden", "pause")


async def test_unknown_action_is_rejected(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="Unsupported power action"):
        await client.change_power("win11-golden", "explode")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


async def test_snapshot_lifecycle(client: WorkstationClient, fake: FakeVmrun, golden: str):
    await client.create_snapshot("win11-golden", "golden")
    assert fake.snapshots[golden] == ["golden"]
    assert await client.list_snapshots("win11-golden") == [{"name": "golden"}]
    await client.revert_snapshot("win11-golden", "golden")
    await client.delete_snapshot("win11-golden", "golden")
    assert fake.snapshots[golden] == []


async def test_delete_snapshot_can_take_children(client: WorkstationClient, fake: FakeVmrun):
    await client.create_snapshot("win11-golden", "golden")
    await client.delete_snapshot("win11-golden", "golden", delete_children=True)
    assert "andDeleteChildren" in fake.methods("deleteSnapshot")[0].args


async def test_reverting_a_missing_snapshot_reports_vmrun_error(client: WorkstationClient):
    with pytest.raises(VmrunError):
        await client.revert_snapshot("win11-golden", "never-taken")


async def test_snapshot_names_are_validated(client: WorkstationClient):
    for bad in ["", "  ", "a\\b", "../escape"]:
        with pytest.raises(InvalidArgumentError):
            await client.create_snapshot("win11-golden", bad)


async def test_a_snapshot_tree_path_is_accepted(client: WorkstationClient, fake: FakeVmrun):
    """vmrun uses 'Parent/Child' to pick one of several same-named snapshots."""
    await client.create_snapshot("win11-golden", "Base/Patched")
    assert fake.methods("snapshot")[0].args[1] == "Base/Patched"


async def test_listing_snapshots_of_a_vm_without_any(client: WorkstationClient):
    assert await client.list_snapshots("ubuntu-dev") == []


async def test_a_snapshot_listing_failure_is_not_reported_as_none(
    client: WorkstationClient, fake: FakeVmrun
):
    """Cannot-tell and there-are-none must not look the same before a revert."""
    fake.fail_commands.add("listSnapshots")
    with pytest.raises(VmrunError):
        await client.list_snapshots("win11-golden")


async def test_get_vm_flags_an_unreadable_snapshot_list(client: WorkstationClient, fake: FakeVmrun):
    fake.fail_commands.add("listSnapshots")
    detail = await client.get_vm("win11-golden")
    assert detail["snapshots"] is None
    assert "simulated failure" in detail["snapshots_error"]


# --------------------------------------------------------------------------- #
# Cloning
# --------------------------------------------------------------------------- #


async def test_linked_clone_renames_the_display_name(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    result = await client.clone_vm(
        "win11-golden",
        "win11-test-01",
        destination_dir=str(vm_root / "clones" / "win11-test-01"),
        clone_type="linked",
        snapshot="golden",
    )
    assert result["status"] == "completed"
    assert Path(result["path"]).is_file()
    assert load_vmx(result["path"]).get("displayname") == "win11-test-01"
    call = fake.methods("clone")[0]
    assert call.args[2] == "linked"
    # vmrun takes these as named options; a bare positional snapshot is rejected.
    assert "-snapshot=golden" in call.args
    assert "-cloneName=win11-test-01" in call.args


async def test_a_clone_without_a_snapshot_passes_no_snapshot_option(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    await client.clone_vm("win11-golden", "plain", destination_dir=str(vm_root / "plain"))
    assert not [arg for arg in fake.methods("clone")[0].args if arg.startswith("-snapshot=")]


async def test_a_snapshot_name_with_spaces_survives(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    await client.clone_vm(
        "win11-golden",
        "spaced",
        destination_dir=str(vm_root / "spaced"),
        snapshot="clean build",
    )
    assert "-snapshot=clean build" in fake.methods("clone")[0].args


async def test_cloning_a_running_vm_without_a_snapshot_is_refused(
    client: WorkstationClient, fake: FakeVmrun, golden: str, vm_root: Path
):
    """VMware can only clone a live VM through a snapshot taken while it was off."""
    fake.running.add(golden)
    with pytest.raises(InvalidArgumentError, match="powered on"):
        await client.clone_vm("win11-golden", "live", destination_dir=str(vm_root / "live"))


async def test_cloning_a_running_vm_from_a_snapshot_is_allowed(
    client: WorkstationClient, fake: FakeVmrun, golden: str, vm_root: Path
):
    fake.running.add(golden)
    result = await client.clone_vm(
        "win11-golden", "live", destination_dir=str(vm_root / "live"), snapshot="golden"
    )
    assert result["status"] == "completed"


async def test_player_cannot_clone_or_snapshot(vm_root: Path, fake: FakeVmrun):
    from vmware_mcp.config import PermissionMode, Product, Settings

    settings = Settings(
        vm_dirs=(vm_root,),
        product=Product.PLAYER,
        permission_mode=PermissionMode.DESTRUCTIVE,
    )
    player = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError, match="does not support cloning"):
        await player.clone_vm("win11-golden", "nope")
    with pytest.raises(InvalidArgumentError, match="does not support snapshots"):
        await player.create_snapshot("win11-golden", "nope")


async def test_clone_defaults_next_to_the_source(client: WorkstationClient, vm_root: Path):
    result = await client.clone_vm("win11-golden", "sibling-clone")
    assert Path(result["path"]).parent.parent == vm_root


async def test_clone_refuses_a_destination_outside_the_library(
    client: WorkstationClient, outside_dir: Path
):
    with pytest.raises(InvalidArgumentError, match="outside the configured VM directories"):
        await client.clone_vm("win11-golden", "escapee", destination_dir=str(outside_dir))


async def test_clone_refuses_to_overwrite(client: WorkstationClient, vm_root: Path):
    await client.clone_vm("win11-golden", "dupe", destination_dir=str(vm_root / "dupe"))
    with pytest.raises(InvalidArgumentError, match="already exists"):
        await client.clone_vm("win11-golden", "dupe", destination_dir=str(vm_root / "dupe"))


async def test_clone_names_are_validated(client: WorkstationClient):
    for bad in ["../escape", "with/slash", ""]:
        with pytest.raises(InvalidArgumentError):
            await client.clone_vm("win11-golden", bad)


async def test_bad_clone_type_is_rejected(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="clone_type"):
        await client.clone_vm("win11-golden", "x", clone_type="sparse")  # type: ignore[arg-type]


async def test_a_failed_clone_leaves_no_empty_directory(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    fake.fail_commands.add("clone")
    target = vm_root / "doomed"
    with pytest.raises(VmrunError):
        await client.clone_vm("win11-golden", "doomed", destination_dir=str(target))
    assert not target.exists(), "an empty folder was left behind for a clone that never existed"


async def test_clone_uses_the_clone_timeout(vm_root: Path, fake: FakeVmrun, monkeypatch):
    client = make_client(vm_root, fake, clone_timeout=999, command_timeout=5)
    seen: dict[str, float | None] = {}
    original = fake.run

    async def spy(command, *args, **kwargs):
        if command == "clone":
            seen["timeout"] = kwargs.get("timeout")
        return await original(command, *args, **kwargs)

    monkeypatch.setattr(fake, "run", spy)
    await client.clone_vm("win11-golden", "timed", destination_dir=str(vm_root / "timed"))
    assert seen["timeout"] == 999


# --------------------------------------------------------------------------- #
# clone_many
# --------------------------------------------------------------------------- #


async def test_clone_many_creates_a_numbered_batch(client: WorkstationClient, vm_root: Path):
    result = await client.clone_many(
        "win11-golden", 3, name_prefix="lab", destination_dir=str(vm_root / "batch")
    )
    assert result["created"] == 3
    assert result["failed"] == 0
    assert [vm["name"] for vm in result["vms"]] == ["lab-01", "lab-02", "lab-03"]


async def test_clone_many_widens_the_number_for_large_batches(
    client: WorkstationClient, vm_root: Path
):
    result = await client.clone_many(
        "win11-golden", 12, name_prefix="wide", destination_dir=str(vm_root / "wide")
    )
    names = [vm["name"] for vm in result["vms"]]
    assert names[0] == "wide-01"
    assert names[-1] == "wide-12"


async def test_clone_many_continues_past_a_failure(client: WorkstationClient, vm_root: Path):
    root = vm_root / "batch"
    blocked = root / "lab-02"
    blocked.mkdir(parents=True)
    (blocked / "lab-02.vmx").write_text('.encoding = "UTF-8"\ndisplayName = "lab-02"\n')

    result = await client.clone_many(
        "win11-golden", 3, name_prefix="lab", destination_dir=str(root)
    )
    assert result["created"] == 2
    assert result["failed"] == 1
    assert result["errors"][0]["name"] == "lab-02"
    assert [vm["name"] for vm in result["vms"]] == ["lab-01", "lab-03"]


async def test_clone_many_keeps_a_clone_that_fails_to_start(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    fake.fail_commands.add("start")
    result = await client.clone_many(
        "win11-golden", 2, name_prefix="boot", destination_dir=str(vm_root / "boot"), start=True
    )
    assert result["created"] == 2, "a power-on failure must not discard the clone"
    assert result["failed"] == 0
    assert all(vm["powered_on"] is False for vm in result["vms"])
    assert all("power_error" in vm for vm in result["vms"])


async def test_clone_many_starts_each_clone(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    result = await client.clone_many(
        "win11-golden", 2, name_prefix="run", destination_dir=str(vm_root / "run"), start=True
    )
    assert all(vm["powered_on"] for vm in result["vms"])
    assert len(fake.methods("start")) == 2


async def test_clone_many_is_sequential_by_default(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    fake.dispatch_delay = 0.02
    await client.clone_many(
        "win11-golden", 3, name_prefix="seq", destination_dir=str(vm_root / "seq")
    )
    assert fake.max_in_flight == 1, "cloning in parallel by default risks source lock errors"


async def test_clone_many_can_be_parallelised(vm_root: Path, fake: FakeVmrun):
    fake.dispatch_delay = 0.05
    client = make_client(vm_root, fake, max_concurrency=4)
    result = await client.clone_many(
        "win11-golden",
        4,
        name_prefix="par",
        destination_dir=str(vm_root / "par"),
        concurrency=4,
    )
    assert result["created"] == 4
    assert fake.max_in_flight > 1


async def test_clone_concurrency_never_exceeds_the_global_cap(vm_root: Path, fake: FakeVmrun):
    fake.dispatch_delay = 0.05
    client = make_client(vm_root, fake, max_concurrency=2)
    await client.clone_many(
        "win11-golden",
        4,
        name_prefix="cap",
        destination_dir=str(vm_root / "cap"),
        concurrency=10,
    )
    assert fake.max_in_flight <= 2


async def test_clone_many_validates_its_inputs(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="at least 1"):
        await client.clone_many("win11-golden", 0, name_prefix="x")
    with pytest.raises(InvalidArgumentError, match="concurrency"):
        await client.clone_many("win11-golden", 1, name_prefix="x", concurrency=0)
    with pytest.raises(InvalidArgumentError, match="name_prefix"):
        await client.clone_many("win11-golden", 1, name_prefix="  ")


async def test_clone_batch_cap_is_configurable(vm_root: Path, fake: FakeVmrun):
    client = make_client(vm_root, fake, max_clone_batch=2)
    with pytest.raises(InvalidArgumentError, match="more than 2"):
        await client.clone_many("win11-golden", 3, name_prefix="x")


# --------------------------------------------------------------------------- #
# Reconfigure and delete
# --------------------------------------------------------------------------- #


async def test_reconfigure_updates_the_vmx(client: WorkstationClient, vm_root: Path):
    result = await client.reconfigure_vm("ubuntu-dev", cpu_count=8, memory_mb=8192)
    assert result["previous"]["cpu_count"] == 2
    assert result["current"]["cpu_count"] == 8
    reloaded = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")
    assert reloaded.get("numvcpus") == "8"
    assert reloaded.get("memsize") == "8192"


async def test_reconfigure_requires_power_off(
    client: WorkstationClient, fake: FakeVmrun, ubuntu: str
):
    fake.running.add(ubuntu)
    with pytest.raises(InvalidArgumentError, match="powered on"):
        await client.reconfigure_vm("ubuntu-dev", cpu_count=8)


async def test_reconfigure_needs_something_to_do(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="Nothing to change"):
        await client.reconfigure_vm("ubuntu-dev")


async def test_reconfigure_validates_the_new_name(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError):
        await client.reconfigure_vm("ubuntu-dev", name="../evil")


async def test_delete_requires_confirmation(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="confirm=true"):
        await client.delete_vm("ubuntu-dev", confirm=False)


async def test_delete_removes_the_vm_and_its_folder(client: WorkstationClient, vm_root: Path):
    folder = vm_root / "ubuntu-dev"
    result = await client.delete_vm("ubuntu-dev", confirm=True)
    assert result["status"] == "completed"
    assert not folder.exists()
    assert {vm["name"] for vm in await client.list_vms()} == {"win11-golden", "win10-legacy"}


async def test_delete_refuses_a_running_vm(client: WorkstationClient, fake: FakeVmrun, ubuntu: str):
    fake.running.add(ubuntu)
    with pytest.raises(InvalidArgumentError, match="powered on"):
        await client.delete_vm("ubuntu-dev", confirm=True)


# --------------------------------------------------------------------------- #
# Screenshots and host file transfer
# --------------------------------------------------------------------------- #


async def test_screenshot_defaults_beside_the_vmx(client: WorkstationClient, vm_root: Path):
    result = await client.screenshot("win11-golden")
    assert Path(result["screenshot"]).is_file()
    assert result["bytes"] > 0
    assert Path(result["screenshot"]).parent == vm_root / "win11-golden"


async def test_screenshot_refuses_to_write_outside_the_allow_list(
    client: WorkstationClient, outside_dir: Path
):
    with pytest.raises(InvalidArgumentError, match="VMWARE_HOST_WRITE_DIRS"):
        await client.screenshot("win11-golden", str(outside_dir / "shot.png"))


async def test_copy_from_guest_respects_the_write_allow_list(
    client: WorkstationClient, fake: FakeVmrun, golden: str, outside_dir: Path
):
    fake.guest_files.setdefault(golden, {})[r"C:\log.txt"] = b"hello"
    with pytest.raises(InvalidArgumentError, match="VMWARE_HOST_WRITE_DIRS"):
        await client.copy_from_guest(
            "win11-golden", r"C:\log.txt", str(outside_dir / "log.txt"), auth=client.auth()
        )


async def test_copy_from_guest_writes_inside_the_allow_list(
    client: WorkstationClient, fake: FakeVmrun, golden: str, vm_root: Path
):
    fake.guest_files.setdefault(golden, {})[r"C:\log.txt"] = b"hello"
    target = vm_root / "out" / "log.txt"
    result = await client.copy_from_guest(
        "win11-golden", r"C:\log.txt", str(target), auth=client.auth()
    )
    assert target.read_bytes() == b"hello"
    assert result["bytes"] == 5


async def test_copy_to_guest_creates_the_target_directory(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path, golden: str
):
    installer = vm_root / "app.msi"
    installer.write_bytes(b"MSI")
    result = await client.copy_to_guest(
        "win11-golden", str(installer), r"C:\Temp\app.msi", auth=client.auth()
    )
    assert result["guest_path"] == r"C:\Temp\app.msi"
    assert fake.guest_dirs[golden] == {r"C:\Temp"}


async def test_copy_to_guest_can_skip_directory_creation(
    client: WorkstationClient, fake: FakeVmrun, vm_root: Path
):
    installer = vm_root / "app.msi"
    installer.write_bytes(b"MSI")
    await client.copy_to_guest(
        "win11-golden",
        str(installer),
        r"C:\Temp\app.msi",
        auth=client.auth(),
        create_parents=False,
    )
    assert not fake.methods("createDirectoryInGuest")


async def test_copy_to_guest_rejects_a_missing_host_file(client: WorkstationClient, vm_root: Path):
    with pytest.raises(InvalidArgumentError, match="does not exist"):
        await client.copy_to_guest(
            "win11-golden", str(vm_root / "nope.msi"), r"C:\Temp\a.msi", auth=client.auth()
        )


async def test_copy_to_guest_respects_the_read_allow_list(
    vm_root: Path, fake: FakeVmrun, tmp_path: Path
):
    allowed = vm_root / "builds"
    allowed.mkdir()
    (allowed / "ok.msi").write_bytes(b"x")
    secret = tmp_path / "secret.key"
    secret.write_bytes(b"x")
    client = make_client(vm_root, fake, host_read_dirs=(allowed,))
    await client.copy_to_guest(
        "win11-golden", str(allowed / "ok.msi"), r"C:\Temp\ok.msi", auth=client.auth()
    )
    with pytest.raises(InvalidArgumentError, match="VMWARE_HOST_READ_DIRS"):
        await client.copy_to_guest(
            "win11-golden", str(secret), r"C:\Temp\secret.key", auth=client.auth()
        )


# --------------------------------------------------------------------------- #
# Batch fan-out
# --------------------------------------------------------------------------- #


@pytest.fixture
def lab(vm_root: Path) -> Path:
    for index in (1, 2, 3):
        write_vmx(vm_root, f"web-test-0{index}", guest_os="windows11-64")
    return vm_root


async def test_power_many_starts_the_matching_set(
    lab: Path, client: WorkstationClient, fake: FakeVmrun
):
    result = await client.power_many("web-test-*", "start")
    assert result["matched"] == 3
    assert result["succeeded"] == 3
    assert result["failed"] == 0
    assert len(fake.methods("start")) == 3


async def test_power_many_dry_run_changes_nothing(
    lab: Path, client: WorkstationClient, fake: FakeVmrun
):
    result = await client.power_many("web-test-*", "start", dry_run=True)
    assert result["dry_run"] is True
    assert result["matched"] == 3
    assert [vm["name"] for vm in result["vms"]] == [
        "web-test-01",
        "web-test-02",
        "web-test-03",
    ]
    assert not fake.methods("start")


async def test_power_many_reports_per_vm_failures(
    lab: Path, client: WorkstationClient, fake: FakeVmrun
):
    fake.fail_commands.add("start")
    result = await client.power_many("web-test-*", "start")
    assert result["succeeded"] == 0
    assert result["failed"] == 3
    assert all("error" in item for item in result["errors"])


async def test_power_many_matching_nothing_is_not_an_error(client: WorkstationClient):
    result = await client.power_many("nothing-matches-this", "start")
    assert result["matched"] == 0
    assert result["succeeded"] == 0


async def test_power_many_rejects_an_empty_pattern(client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="pattern must not be empty"):
        await client.power_many("   ", "start")


async def test_revert_many_stops_running_vms_first(
    lab: Path, client: WorkstationClient, fake: FakeVmrun
):
    for index in (1, 2, 3):
        path = str(lab / f"web-test-0{index}" / f"web-test-0{index}.vmx")
        fake.snapshots[path] = ["golden"]
        fake.running.add(path)
    result = await client.revert_many("web-test-*", "golden")
    assert result["succeeded"] == 3
    assert len(fake.methods("stop")) == 3
    assert len(fake.methods("revertToSnapshot")) == 3


async def test_revert_many_dry_run(lab: Path, client: WorkstationClient, fake: FakeVmrun):
    result = await client.revert_many("web-test-*", "golden", dry_run=True)
    assert result["dry_run"] is True
    assert not fake.methods("revertToSnapshot")


async def test_delete_many_requires_confirmation(lab: Path, client: WorkstationClient):
    with pytest.raises(InvalidArgumentError, match="confirm=true"):
        await client.delete_many("web-test-*", confirm=False)


async def test_delete_many_dry_run_needs_no_confirmation(
    lab: Path, client: WorkstationClient, fake: FakeVmrun
):
    result = await client.delete_many("web-test-*", confirm=False, dry_run=True)
    assert result["matched"] == 3
    assert not fake.methods("deleteVM")


async def test_delete_many_removes_everything_that_matched(lab: Path, client: WorkstationClient):
    result = await client.delete_many("web-test-*", confirm=True)
    assert result["succeeded"] == 3
    remaining = {vm["name"] for vm in await client.list_vms()}
    assert remaining == {"win11-golden", "ubuntu-dev", "win10-legacy"}


async def test_delete_many_stops_running_vms(lab: Path, client: WorkstationClient, fake: FakeVmrun):
    fake.running.add(str(lab / "web-test-01" / "web-test-01.vmx"))
    result = await client.delete_many("web-test-01", confirm=True)
    assert result["succeeded"] == 1
    assert fake.methods("stop")


async def test_batch_operations_run_concurrently(lab: Path, vm_root: Path, fake: FakeVmrun):
    fake.dispatch_delay = 0.05
    client = make_client(vm_root, fake, max_concurrency=3)
    await client.power_many("web-test-*", "start")
    assert fake.max_in_flight > 1, "batch work should overlap"


async def test_batch_concurrency_respects_the_configured_cap(
    lab: Path, vm_root: Path, fake: FakeVmrun
):
    fake.dispatch_delay = 0.05
    client = make_client(vm_root, fake, max_concurrency=2)
    await client.power_many("web-test-*", "start")
    assert fake.max_in_flight <= 2


async def test_unknown_vm_is_reported_clearly(client: WorkstationClient):
    with pytest.raises(ObjectNotFoundError, match="No VM matches"):
        await client.change_power("ghost", "start")
