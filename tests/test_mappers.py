from __future__ import annotations

from datetime import datetime, timezone

from pyVmomi import vim

from vmware_mcp.vsphere import mappers
from vmware_mcp.vsphere.query import InventoryNode, InventoryPathIndex, ObjectRecord


def record(moid: str, vim_type: type = vim.VirtualMachine, **props) -> ObjectRecord:
    return ObjectRecord(obj=vim_type(moid, None), moid=moid, type=vim_type.__name__, props=props)


def test_scalar_helpers():
    assert mappers.gib(1073741824) == 1.0
    assert mappers.gib(None) is None
    assert mappers.percent(25, 100) == 25.0
    assert mappers.percent(1, 0) is None
    assert mappers.percent(None, 10) is None
    assert mappers.as_text(None) is None
    assert mappers.as_text(vim.VirtualMachinePowerState.poweredOn) == "poweredOn"
    moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    assert mappers.as_timestamp(moment) == "2026-01-02T03:04:00+00:00"


def test_vm_summary_reports_sizes_in_both_units():
    vm = mappers.map_vm_summary(
        record(
            "vm-1",
            name="web-01",
            **{
                "config.hardware.memoryMB": 8192,
                "config.hardware.numCPU": 4,
                "runtime.powerState": vim.VirtualMachinePowerState.poweredOn,
                "summary.storage.committed": 64424509440,
            },
        )
    )
    assert vm["memory_mb"] == 8192
    assert vm["memory_gib"] == 8.0
    assert vm["committed_storage_gib"] == 60.0
    assert vm["power_state"] == "poweredOn"
    assert vm["is_template"] is False


def test_vm_summary_tolerates_properties_the_collector_did_not_return():
    # A powered-off VM has no guest data, and an unprivileged account may not
    # see config at all; every field must still be present and null.
    vm = mappers.map_vm_summary(record("vm-1", name="ghost"))
    assert vm["name"] == "ghost"
    assert vm["cpu_count"] is None
    assert vm["ip_address"] is None
    assert vm["memory_gib"] is None


def test_vm_detail_maps_hardware_devices():
    disk = vim.vm.device.VirtualDisk(
        key=2000,
        capacityInKB=104857600,
        deviceInfo=vim.Description(label="Hard disk 1", summary=""),
        backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
            fileName="[ds-nvme] web-01/web-01.vmdk",
            diskMode="persistent",
            thinProvisioned=True,
            uuid="disk-uuid",
        ),
    )
    nic = vim.vm.device.VirtualVmxnet3(
        key=4000,
        macAddress="00:50:56:aa:bb:cc",
        deviceInfo=vim.Description(label="Network adapter 1", summary=""),
        backing=vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName="VM Network"),
        connectable=vim.vm.device.VirtualDevice.ConnectInfo(connected=True, startConnected=True),
    )
    cdrom = vim.vm.device.VirtualCdrom(key=3000)

    detail = mappers.map_vm_detail(
        record("vm-1", name="web-01", **{"config.hardware.device": [disk, nic, cdrom]})
    )
    assert detail["disks"] == [
        {
            "key": 2000,
            "label": "Hard disk 1",
            "capacity_gib": 100.0,
            "file_name": "[ds-nvme] web-01/web-01.vmdk",
            "disk_mode": "persistent",
            "thin_provisioned": True,
            "datastore_moid": None,
            "uuid": "disk-uuid",
        }
    ]
    assert detail["network_adapters"][0]["mac_address"] == "00:50:56:aa:bb:cc"
    assert detail["network_adapters"][0]["network"] == "VM Network"
    assert detail["network_adapters"][0]["connected"] is True
    assert detail["cdroms"] == 1


def test_guest_disk_usage_is_derived_from_capacity_and_free():
    guest_disk = vim.vm.GuestInfo.DiskInfo(
        diskPath="/", capacity=107374182400, freeSpace=26843545600, filesystemType="ext4"
    )
    mapped = mappers.map_guest_disk(guest_disk)
    assert mapped == {
        "mount_point": "/",
        "capacity_gib": 100.0,
        "free_gib": 25.0,
        "used_percent": 75.0,
        "filesystem": "ext4",
    }


def build_snapshot_tree():
    created = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    child = vim.vm.SnapshotTree(
        snapshot=vim.vm.Snapshot("snapshot-3", None),
        vm=vim.VirtualMachine("vm-1", None),
        name="after-patch",
        description="",
        id=3,
        createTime=created,
        state=vim.VirtualMachinePowerState.poweredOff,
        quiesced=False,
    )
    root = vim.vm.SnapshotTree(
        snapshot=vim.vm.Snapshot("snapshot-2", None),
        vm=vim.VirtualMachine("vm-1", None),
        name="before-patch",
        description="pre-upgrade",
        id=2,
        createTime=created,
        state=vim.VirtualMachinePowerState.poweredOn,
        quiesced=True,
        childSnapshotList=[child],
    )
    return vim.vm.SnapshotInfo(
        currentSnapshot=vim.vm.Snapshot("snapshot-3", None), rootSnapshotList=[root]
    )


def test_snapshot_tree_is_mapped_with_the_current_pointer():
    info = mappers.map_snapshot_info(build_snapshot_tree())
    assert info["count"] == 2
    assert info["current_snapshot_moid"] == "snapshot-3"
    root = info["tree"][0]
    assert root["name"] == "before-patch"
    assert root["quiesced"] is True
    assert root["is_current"] is False
    assert root["children"][0]["is_current"] is True


def test_snapshots_flatten_to_parent_child_paths():
    info = mappers.map_snapshot_info(build_snapshot_tree())
    flattened = mappers.flatten_snapshots(info["tree"])
    assert [node["path"] for node in flattened] == [
        "before-patch",
        "before-patch/after-patch",
    ]
    assert "children" not in flattened[0]


def test_no_snapshots_maps_to_none():
    assert mappers.map_snapshot_info(None) is None


def test_host_utilisation_percentages():
    host = mappers.map_host(
        record(
            "host-11",
            vim.HostSystem,
            name="esxi-01",
            **{
                "summary.hardware.numCpuCores": 24,
                "summary.hardware.cpuMhz": 2000,
                "summary.hardware.memorySize": 274877906944,  # 256 GiB
                "summary.quickStats.overallCpuUsage": 12000,
                "summary.quickStats.overallMemoryUsage": 131072,  # 128 GiB in MiB
                "runtime.connectionState": vim.HostSystem.ConnectionState.connected,
            },
        )
    )
    assert host["cpu_total_mhz"] == 48000
    assert host["cpu_used_percent"] == 25.0
    assert host["memory_gib"] == 256.0
    assert host["memory_used_gib"] == 128.0
    assert host["memory_used_percent"] == 50.0
    assert host["connection_state"] == "connected"


def test_datastore_flags_overprovisioning():
    datastore = mappers.map_datastore(
        record(
            "datastore-21",
            vim.Datastore,
            name="ds-nvme",
            **{
                "summary.capacity": 1099511627776,  # 1 TiB
                "summary.freeSpace": 109951162777,
                "summary.uncommitted": 549755813888,
                "summary.type": "VMFS",
            },
        )
    )
    assert datastore["capacity_gib"] == 1024.0
    assert datastore["used_percent"] == 90.0
    assert datastore["overprovisioned"] is True


def test_datastore_without_summary_does_not_claim_overprovisioning():
    datastore = mappers.map_datastore(record("datastore-9", vim.Datastore, name="ds-unknown"))
    assert datastore["used_percent"] is None
    assert datastore["overprovisioned"] is None


def test_cluster_capacity_and_features():
    cluster = mappers.map_cluster(
        record(
            "domain-c7",
            vim.ClusterComputeResource,
            name="Cluster-A",
            **{
                "summary.totalCpu": 96000,
                "summary.effectiveCpu": 72000,
                "summary.totalMemory": 549755813888,
                "summary.effectiveMemory": 393216,
                "configuration.drsConfig.enabled": True,
                "configuration.dasConfig.enabled": False,
            },
        )
    )
    assert cluster["cpu_used_percent"] == 25.0
    assert cluster["memory_total_gib"] == 512.0
    assert cluster["memory_available_gib"] == 384.0
    assert cluster["drs_enabled"] is True
    assert cluster["ha_enabled"] is False


def test_distributed_portgroup_vlan_modes():
    access = mappers.map_dvportgroup_extras(
        record(
            "dvportgroup-32",
            vim.dvs.DistributedVirtualPortgroup,
            **{
                "config.numPorts": 128,
                "config.defaultPortConfig": (
                    vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                        vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(vlanId=120)
                    )
                ),
            },
        )
    )
    assert access["vlan"] == {"mode": "access", "vlan_id": 120}
    assert access["port_count"] == 128

    trunk = mappers.map_dvportgroup_extras(
        record(
            "dvportgroup-33",
            vim.dvs.DistributedVirtualPortgroup,
            **{
                "config.defaultPortConfig": (
                    vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                        vlan=vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec(
                            vlanId=[vim.NumericRange(start=100, end=200)]
                        )
                    )
                ),
            },
        )
    )
    assert trunk["vlan"] == {"mode": "trunk", "ranges": [{"start": 100, "end": 200}]}


def test_performance_series_summary_ignores_negative_samples():
    # vSphere uses -1 to mean "no data for this sample".
    series = mappers.map_performance_series("cpu.usage.average", "percent", [10.0, -1, 30.0, 20.0])
    assert series == {
        "counter": "cpu.usage.average",
        "unit": "percent",
        "samples": 3,
        "latest": 20.0,
        "average": 20.0,
        "minimum": 10.0,
        "maximum": 30.0,
    }


def test_performance_series_with_no_samples():
    series = mappers.map_performance_series("mem.usage.average", "percent", [])
    assert series["samples"] == 0
    assert series["average"] is None


def test_inventory_path_index_renders_paths_and_datacenters():
    index = InventoryPathIndex(
        {
            "group-d1": InventoryNode("Datacenters", "vim.Folder", None),
            "datacenter-2": InventoryNode("DC1", "vim.Datacenter", "group-d1"),
            "group-v3": InventoryNode("vm", "vim.Folder", "datacenter-2"),
            "group-v9": InventoryNode("Tier1", "vim.Folder", "group-v3"),
        }
    )
    assert index.path_of("group-v9", "web-01") == "/DC1/vm/Tier1/web-01"
    assert index.datacenter_of("group-v9") == "DC1"
    assert index.name_of("group-v9") == "Tier1"
    assert index.path_of(None) is None
    assert index.path_of("does-not-exist") is None
    assert index.datacenter_of("does-not-exist") is None


def test_inventory_path_index_survives_a_parent_cycle():
    # Defensive: a malformed reply must not hang path resolution.
    index = InventoryPathIndex(
        {
            "a": InventoryNode("A", "vim.Folder", "b"),
            "b": InventoryNode("B", "vim.Folder", "a"),
        }
    )
    assert index.path_of("a", "leaf") == "/B/A/leaf"


def test_task_info_mapping_extracts_the_error_message():
    info = vim.TaskInfo(
        key="task-9",
        task=vim.Task("task-9", None),
        descriptionId="VirtualMachine.powerOn",
        state=vim.TaskInfo.State.error,
        progress=100,
        entityName="web-01",
        cancelable=False,
        error=vim.fault.InvalidState(msg="The attempted operation cannot be performed."),
        reason=vim.TaskReasonUser(userName="svc@vsphere.local"),
        queueTime=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    mapped = mappers.map_task_info(info)
    assert mapped["task_id"] == "task-9"
    assert mapped["state"] == "error"
    assert mapped["error"] == "The attempted operation cannot be performed."
    assert mapped["initiated_by"] == "svc@vsphere.local"
