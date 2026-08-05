"""Power, snapshot and lifecycle tools, including the permission gate."""

from __future__ import annotations

import pytest

from conftest import call_ok, call_tool, error_text
from vmware_mcp.config import PermissionMode

WRITE_TOOLS = [
    ("vsphere_change_vm_power_state", {"vm": "web-01", "action": "power_off"}),
    ("vsphere_create_snapshot", {"vm": "web-01", "name": "snap"}),
    ("vsphere_clone_vm", {"vm": "web-01", "name": "web-02"}),
    ("vsphere_reconfigure_vm", {"vm": "web-01", "cpu_count": 8}),
    ("vsphere_migrate_vm", {"vm": "web-01", "host": "esxi-02.lab.local"}),
]

DESTRUCTIVE_TOOLS = [
    ("vsphere_delete_vm", {"vm": "db-01", "confirm": True}),
    ("vsphere_revert_to_snapshot", {"vm": "web-01"}),
    ("vsphere_delete_snapshot", {"vm": "web-01", "snapshot": "before-patch"}),
]


@pytest.mark.parametrize(("tool", "arguments"), WRITE_TOOLS + DESTRUCTIVE_TOOLS)
async def test_read_only_mode_refuses_every_mutating_tool(server_factory, tool, arguments):
    server = server_factory(PermissionMode.READ_ONLY)
    result = await call_tool(server, tool, **arguments)
    assert result.is_error
    message = error_text(result)
    assert "read-only" in message
    assert "VMWARE_PERMISSION_MODE=" in message


@pytest.mark.parametrize(("tool", "arguments"), DESTRUCTIVE_TOOLS)
async def test_write_mode_still_refuses_destructive_tools(server_factory, tool, arguments):
    server = server_factory(PermissionMode.WRITE)
    result = await call_tool(server, tool, **arguments)
    assert result.is_error
    assert "requires permission mode 'destructive'" in error_text(result)


@pytest.mark.parametrize(("tool", "arguments"), WRITE_TOOLS)
async def test_write_mode_allows_write_tools(server_factory, tool, arguments):
    server = server_factory(PermissionMode.WRITE)
    result = await call_tool(server, tool, **arguments)
    assert not result.is_error, error_text(result)


async def test_nothing_is_sent_to_vcenter_when_permission_is_denied(server_factory, fake_session):
    server = server_factory(PermissionMode.READ_ONLY)
    await call_tool(server, "vsphere_delete_vm", vm="db-01", confirm=True)
    assert fake_session.inventory.stub.methods("Destroy_Task") == []


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #


@pytest.fixture
def rw_server(server_factory):
    return server_factory(PermissionMode.WRITE)


@pytest.fixture
def admin_server(server_factory):
    return server_factory(PermissionMode.DESTRUCTIVE)


async def test_power_off_invokes_the_task_and_waits_for_it(rw_server, fake_session):
    result = await call_ok(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="power_off"
    )
    assert result["status"] == "completed"
    assert result["previous_power_state"] == "poweredOn"
    assert result["task"]["state"] == "success"
    calls = fake_session.inventory.stub.methods("PowerOffVM_Task")
    assert [call.moid for call in calls] == ["vm-101"]


async def test_power_on_an_already_running_vm_changes_nothing(rw_server, fake_session):
    result = await call_ok(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="power_on"
    )
    assert result["status"] == "no_change"
    assert "already poweredOn" in result["message"]
    assert fake_session.inventory.stub.methods("PowerOnVM_Task") == []


async def test_power_on_a_stopped_vm_starts_it(rw_server, fake_session):
    result = await call_ok(
        rw_server, "vsphere_change_vm_power_state", vm="db-01", action="power_on"
    )
    assert result["status"] == "completed"
    assert [call.moid for call in fake_session.inventory.stub.methods("PowerOnVM_Task")] == [
        "vm-102"
    ]


async def test_not_waiting_returns_a_task_id_to_poll(rw_server):
    result = await call_ok(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="reset", wait=False
    )
    assert result["status"] == "running"
    assert result["waited"] is False
    assert result["task_id"].startswith("task-")


async def test_a_failed_task_is_reported_as_an_error(rw_server, fake_session):
    fake_session.inventory.task_state = "error"
    result = await call_tool(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="power_off"
    )
    assert result.is_error
    assert "it broke" in error_text(result)


async def test_guest_shutdown_requires_vmware_tools(rw_server, fake_session):
    result = await call_tool(
        rw_server, "vsphere_change_vm_power_state", vm="db-01", action="shutdown_guest"
    )
    assert result.is_error
    message = error_text(result)
    assert "needs VMware Tools" in message
    assert "'power_off' or 'reset'" in message
    assert fake_session.inventory.stub.methods("ShutdownGuest") == []


async def test_guest_shutdown_is_fire_and_forget(rw_server, fake_session):
    result = await call_ok(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="shutdown_guest"
    )
    assert result["status"] == "requested"
    assert result["waited"] is False
    assert [call.moid for call in fake_session.inventory.stub.methods("ShutdownGuest")] == [
        "vm-101"
    ]


async def test_unknown_power_action_is_rejected_by_the_schema(rw_server):
    result = await call_tool(
        rw_server, "vsphere_change_vm_power_state", vm="web-01", action="disintegrate"
    )
    assert result.is_error


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


async def test_list_snapshots_returns_a_tree_and_a_flat_list(server):
    result = await call_ok(server, "vsphere_list_snapshots", vm="web-01")
    assert result["count"] == 2
    assert [snapshot["path"] for snapshot in result["snapshots"]] == [
        "before-patch",
        "before-patch/after-patch",
    ]
    assert result["current_snapshot_moid"] == "snapshot-3"
    assert result["tree"][0]["children"][0]["is_current"] is True


async def test_list_snapshots_on_a_vm_without_any(server):
    result = await call_ok(server, "vsphere_list_snapshots", vm="db-01")
    assert result["count"] == 0
    assert result["snapshots"] == []


async def test_create_snapshot_passes_the_options_through(rw_server, fake_session):
    result = await call_ok(
        rw_server,
        "vsphere_create_snapshot",
        vm="db-01",
        name="pre-upgrade",
        description="before the 9.4 upgrade",
        include_memory=True,
        quiesce=True,
    )
    assert result["status"] == "completed"
    call = fake_session.inventory.stub.methods("CreateSnapshot_Task")[0]
    assert call.moid == "vm-102"
    assert call.args["name"] == "pre-upgrade"
    assert call.args["description"] == "before the 9.4 upgrade"
    assert call.args["memory"] is True
    assert call.args["quiesce"] is True


async def test_create_snapshot_rejects_a_blank_name(rw_server):
    result = await call_tool(rw_server, "vsphere_create_snapshot", vm="db-01", name="   ")
    assert result.is_error
    assert "name must not be empty" in error_text(result)


async def test_revert_defaults_to_the_current_snapshot(admin_server, fake_session):
    result = await call_ok(admin_server, "vsphere_revert_to_snapshot", vm="web-01")
    assert result["snapshot"] == "after-patch"
    assert result["snapshot_moid"] == "snapshot-3"
    call = fake_session.inventory.stub.methods("RevertToSnapshot_Task")[0]
    assert call.moid == "snapshot-3"
    assert call.args["suppressPowerOn"] is False


async def test_revert_accepts_a_snapshot_name_or_path(admin_server, fake_session):
    await call_ok(admin_server, "vsphere_revert_to_snapshot", vm="web-01", snapshot="before-patch")
    await call_ok(
        admin_server,
        "vsphere_revert_to_snapshot",
        vm="web-01",
        snapshot="before-patch/after-patch",
    )
    calls = [call.moid for call in fake_session.inventory.stub.methods("RevertToSnapshot_Task")]
    assert calls == ["snapshot-2", "snapshot-3"]


async def test_unknown_snapshot_lists_what_is_available(admin_server):
    result = await call_tool(
        admin_server, "vsphere_revert_to_snapshot", vm="web-01", snapshot="nope"
    )
    assert result.is_error
    message = error_text(result)
    assert "No snapshot of 'web-01' matches 'nope'" in message
    assert "before-patch/after-patch" in message


async def test_snapshot_operations_on_a_vm_without_snapshots(admin_server):
    result = await call_tool(admin_server, "vsphere_revert_to_snapshot", vm="db-01")
    assert result.is_error
    assert "has no snapshots" in error_text(result)


async def test_delete_snapshot_can_remove_children(admin_server, fake_session):
    await call_ok(
        admin_server,
        "vsphere_delete_snapshot",
        vm="web-01",
        snapshot="before-patch",
        remove_children=True,
    )
    call = fake_session.inventory.stub.methods("RemoveSnapshot_Task")[0]
    assert call.moid == "snapshot-2"
    assert call.args["removeChildren"] is True


async def test_delete_all_snapshots(admin_server, fake_session):
    result = await call_ok(admin_server, "vsphere_delete_snapshot", vm="web-01", delete_all=True)
    assert result["deleted"] == 2
    assert fake_session.inventory.stub.methods("RemoveAllSnapshots_Task")[0].moid == "vm-101"


async def test_delete_all_snapshots_when_there_are_none(admin_server):
    result = await call_tool(admin_server, "vsphere_delete_snapshot", vm="db-01", delete_all=True)
    assert result.is_error
    assert "no snapshots to delete" in error_text(result)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def test_clone_defaults_placement_to_the_source(rw_server, fake_session):
    result = await call_ok(rw_server, "vsphere_clone_vm", vm="web-01", name="web-02")
    assert result["placement"] == {
        "folder_moid": "group-v9",
        "host_moid": None,
        "datastore_moid": None,
        "resource_pool_moid": "resgroup-8",
    }
    call = fake_session.inventory.stub.methods("CloneVM_Task")[0]
    assert call.moid == "vm-101"
    assert call.args["name"] == "web-02"
    assert call.args["folder"]._moId == "group-v9"
    assert call.args["spec"].location.pool._moId == "resgroup-8"


async def test_clone_honours_explicit_placement(rw_server, fake_session):
    await call_ok(
        rw_server,
        "vsphere_clone_vm",
        vm="web-01",
        name="web-03",
        host="esxi-02.lab.local",
        datastore="ds-nfs",
        power_on=True,
    )
    spec = fake_session.inventory.stub.methods("CloneVM_Task")[0].args["spec"]
    assert spec.location.host._moId == "host-12"
    assert spec.location.datastore._moId == "datastore-22"
    assert spec.powerOn is True


async def test_clone_cannot_power_on_a_template(rw_server):
    result = await call_tool(
        rw_server, "vsphere_clone_vm", vm="web-01", name="t1", power_on=True, as_template=True
    )
    assert result.is_error
    assert "cannot be powered on" in error_text(result)


async def test_reconfigure_only_sends_the_supplied_fields(rw_server, fake_session):
    result = await call_ok(
        rw_server, "vsphere_reconfigure_vm", vm="db-01", cpu_count=16, memory_mb=65536
    )
    assert result["previous"] == {"cpu_count": 8, "memory_mb": 32768}
    spec = fake_session.inventory.stub.methods("ReconfigVM_Task")[0].args["spec"]
    assert spec.numCPUs == 16
    assert spec.memoryMB == 65536
    assert spec.annotation is None


async def test_reconfigure_requires_something_to_change(rw_server):
    result = await call_tool(rw_server, "vsphere_reconfigure_vm", vm="db-01")
    assert result.is_error
    assert "Nothing to change" in error_text(result)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"cpu_count": 0}, "cpu_count must be at least 1"),
        ({"memory_mb": 1}, "memory_mb must be at least 4"),
        ({"cpu_count": 6, "cores_per_socket": 4}, "must be a multiple of cores_per_socket"),
    ],
)
async def test_reconfigure_validates_before_calling_vcenter(
    rw_server, fake_session, arguments, expected
):
    result = await call_tool(rw_server, "vsphere_reconfigure_vm", vm="db-01", **arguments)
    assert result.is_error
    assert expected in error_text(result)
    assert fake_session.inventory.stub.methods("ReconfigVM_Task") == []


async def test_migrate_needs_a_destination(rw_server):
    result = await call_tool(rw_server, "vsphere_migrate_vm", vm="web-01")
    assert result.is_error
    assert "at least one of host, datastore or resource_pool" in error_text(result)


async def test_migrate_builds_a_relocate_spec(rw_server, fake_session):
    await call_ok(
        rw_server,
        "vsphere_migrate_vm",
        vm="web-01",
        host="esxi-02.lab.local",
        datastore="ds-nfs",
        priority="high",
    )
    call = fake_session.inventory.stub.methods("RelocateVM_Task")[0]
    assert call.moid == "vm-101"
    assert call.args["spec"].host._moId == "host-12"
    assert call.args["spec"].datastore._moId == "datastore-22"
    assert str(call.args["priority"]) == "highPriority"


async def test_delete_vm_requires_explicit_confirmation(admin_server, fake_session):
    result = await call_tool(admin_server, "vsphere_delete_vm", vm="db-01", confirm=False)
    assert result.is_error
    assert "confirm=true" in error_text(result)
    assert fake_session.inventory.stub.methods("Destroy_Task") == []


async def test_delete_vm_refuses_a_running_vm(admin_server, fake_session):
    result = await call_tool(admin_server, "vsphere_delete_vm", vm="web-01", confirm=True)
    assert result.is_error
    assert "powered on" in error_text(result)
    assert fake_session.inventory.stub.methods("Destroy_Task") == []


async def test_delete_vm_destroys_a_stopped_vm(admin_server, fake_session):
    result = await call_ok(admin_server, "vsphere_delete_vm", vm="db-01", confirm=True)
    assert result["status"] == "completed"
    assert [call.moid for call in fake_session.inventory.stub.methods("Destroy_Task")] == ["vm-102"]
