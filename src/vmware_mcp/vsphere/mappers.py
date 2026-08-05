"""Pure translation of vSphere properties into JSON-friendly dictionaries.

Nothing in here performs I/O: every function takes an already-retrieved
:class:`~vmware_mcp.vsphere.query.ObjectRecord` (or a vSphere data object) and
returns plain data. That keeps the interesting logic unit-testable without a
vCenter to point at.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pyVmomi import vim

from .query import InventoryPathIndex, ObjectRecord, moid_of

BYTES_PER_GIB = 1024**3
KB_PER_GIB = 1024**2


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #


def as_text(value: Any) -> str | None:
    """Stringify vSphere enums (which are not JSON serialisable) and leave None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def as_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return as_text(value)


def gib(value: int | float | None, divisor: int = BYTES_PER_GIB) -> float | None:
    if value is None:
        return None
    return round(value / divisor, 2)


def percent(used: int | float | None, total: int | float | None) -> float | None:
    if used is None or not total:
        return None
    return round(100.0 * used / total, 1)


def _moid(value: Any) -> str | None:
    return moid_of(value)


# --------------------------------------------------------------------------- #
# Virtual machines
# --------------------------------------------------------------------------- #

VM_SUMMARY_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "resourcePool",
    "overallStatus",
    "config.uuid",
    "config.instanceUuid",
    "config.template",
    "config.guestFullName",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "runtime.powerState",
    "runtime.connectionState",
    "runtime.host",
    "runtime.bootTime",
    "guest.hostName",
    "guest.ipAddress",
    "guest.guestState",
    "guest.toolsStatus",
    "guest.toolsRunningStatus",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.guestMemoryUsage",
    "summary.quickStats.uptimeSeconds",
    "summary.storage.committed",
    "summary.storage.uncommitted",
)

VM_DETAIL_PROPERTIES: tuple[str, ...] = VM_SUMMARY_PROPERTIES + (
    "config.annotation",
    "config.version",
    "config.firmware",
    "config.files.vmPathName",
    "config.hardware.device",
    "config.cpuAllocation",
    "config.memoryAllocation",
    "guest.net",
    "guest.disk",
    "guest.toolsVersionStatus2",
    "guest.guestFamily",
    "datastore",
    "network",
    "snapshot",
)


def map_vm_summary(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    """Compact VM view suitable for listings."""
    name = record.get("name")
    parent_moid = _moid(record.props.get("parent"))
    host_moid = _moid(record.props.get("runtime.host"))
    memory_mb = record.get("config.hardware.memoryMB")
    return {
        "moid": record.moid,
        "name": name,
        "path": index.path_of(parent_moid, name) if index else None,
        "datacenter": index.datacenter_of(parent_moid) if index else None,
        "power_state": as_text(record.get("runtime.powerState")),
        "connection_state": as_text(record.get("runtime.connectionState")),
        "overall_status": as_text(record.get("overallStatus")),
        "is_template": bool(record.get("config.template", False)),
        "guest_os": record.get("config.guestFullName"),
        "guest_state": as_text(record.get("guest.guestState")),
        "guest_hostname": record.get("guest.hostName"),
        "ip_address": record.get("guest.ipAddress"),
        "cpu_count": record.get("config.hardware.numCPU"),
        "memory_mb": memory_mb,
        "memory_gib": gib(memory_mb, 1024),
        "cpu_usage_mhz": record.get("summary.quickStats.overallCpuUsage"),
        "guest_memory_usage_mb": record.get("summary.quickStats.guestMemoryUsage"),
        "uptime_seconds": record.get("summary.quickStats.uptimeSeconds"),
        "committed_storage_gib": gib(record.get("summary.storage.committed")),
        "uncommitted_storage_gib": gib(record.get("summary.storage.uncommitted")),
        "tools_status": as_text(record.get("guest.toolsStatus")),
        "tools_running": as_text(record.get("guest.toolsRunningStatus")),
        "host_moid": host_moid,
        "host": index.name_of(host_moid) if index else None,
        "uuid": record.get("config.uuid"),
        "instance_uuid": record.get("config.instanceUuid"),
        "boot_time": as_timestamp(record.get("runtime.bootTime")),
    }


def map_vm_detail(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    """Full VM view: hardware inventory, guest networking, disks, snapshots."""
    detail = map_vm_summary(record, index)
    devices = record.get("config.hardware.device", []) or []
    detail.update(
        {
            "annotation": record.get("config.annotation"),
            "hardware_version": record.get("config.version"),
            "firmware": as_text(record.get("config.firmware")),
            "vmx_path": record.get("config.files.vmPathName"),
            "guest_family": as_text(record.get("guest.guestFamily")),
            "tools_version_status": as_text(record.get("guest.toolsVersionStatus2")),
            "cpu_allocation": map_resource_allocation(record.props.get("config.cpuAllocation")),
            "memory_allocation": map_resource_allocation(
                record.props.get("config.memoryAllocation")
            ),
            "disks": [map_virtual_disk(device) for device in devices if _is_disk(device)],
            "network_adapters": [map_nic(device) for device in devices if _is_nic(device)],
            "cdroms": sum(
                1 for device in devices if isinstance(device, vim.vm.device.VirtualCdrom)
            ),
            "guest_networks": [map_guest_nic(nic) for nic in record.get("guest.net", []) or []],
            "guest_disks": [map_guest_disk(disk) for disk in record.get("guest.disk", []) or []],
            "datastore_moids": [
                _moid(datastore) for datastore in record.get("datastore", []) or []
            ],
            "network_moids": [_moid(network) for network in record.get("network", []) or []],
            "snapshots": map_snapshot_info(record.props.get("snapshot")),
        }
    )
    return detail


def _is_disk(device: Any) -> bool:
    return isinstance(device, vim.vm.device.VirtualDisk)


def _is_nic(device: Any) -> bool:
    return isinstance(device, vim.vm.device.VirtualEthernetCard)


def map_resource_allocation(allocation: Any) -> dict[str, Any] | None:
    if allocation is None:
        return None
    shares = getattr(allocation, "shares", None)
    return {
        "reservation": getattr(allocation, "reservation", None),
        "limit": getattr(allocation, "limit", None),
        "shares_level": as_text(getattr(shares, "level", None)) if shares else None,
        "shares": getattr(shares, "shares", None) if shares else None,
    }


def map_virtual_disk(device: Any) -> dict[str, Any]:
    backing = getattr(device, "backing", None)
    capacity_kb = getattr(device, "capacityInKB", None)
    return {
        "key": getattr(device, "key", None),
        "label": getattr(getattr(device, "deviceInfo", None), "label", None),
        "capacity_gib": gib(capacity_kb, KB_PER_GIB),
        "file_name": getattr(backing, "fileName", None),
        "disk_mode": as_text(getattr(backing, "diskMode", None)),
        "thin_provisioned": getattr(backing, "thinProvisioned", None),
        "datastore_moid": _moid(getattr(backing, "datastore", None)),
        "uuid": getattr(backing, "uuid", None),
    }


def map_nic(device: Any) -> dict[str, Any]:
    backing = getattr(device, "backing", None)
    network_name = getattr(backing, "deviceName", None)
    port = getattr(backing, "port", None)
    connectable = getattr(device, "connectable", None)
    return {
        "key": getattr(device, "key", None),
        "label": getattr(getattr(device, "deviceInfo", None), "label", None),
        "type": type(device).__name__,
        "mac_address": getattr(device, "macAddress", None),
        "network": network_name,
        "portgroup_key": getattr(port, "portgroupKey", None) if port else None,
        "dvs_uuid": getattr(port, "switchUuid", None) if port else None,
        "connected": getattr(connectable, "connected", None) if connectable else None,
        "start_connected": getattr(connectable, "startConnected", None) if connectable else None,
    }


def map_guest_nic(nic: Any) -> dict[str, Any]:
    ip_config = getattr(nic, "ipConfig", None)
    addresses = getattr(ip_config, "ipAddress", None) or []
    return {
        "network": getattr(nic, "network", None),
        "mac_address": getattr(nic, "macAddress", None),
        "connected": getattr(nic, "connected", None),
        "ip_addresses": [
            {
                "address": getattr(address, "ipAddress", None),
                "prefix_length": getattr(address, "prefixLength", None),
                "state": as_text(getattr(address, "state", None)),
            }
            for address in addresses
        ]
        or [{"address": address} for address in (getattr(nic, "ipAddress", None) or [])],
    }


def map_guest_disk(disk: Any) -> dict[str, Any]:
    capacity = getattr(disk, "capacity", None)
    free = getattr(disk, "freeSpace", None)
    used = None if capacity is None or free is None else capacity - free
    return {
        "mount_point": getattr(disk, "diskPath", None),
        "capacity_gib": gib(capacity),
        "free_gib": gib(free),
        "used_percent": percent(used, capacity),
        "filesystem": getattr(disk, "filesystemType", None),
    }


def map_snapshot_info(snapshot_info: Any) -> dict[str, Any] | None:
    """Flatten ``vim.vm.SnapshotInfo`` into a tree plus the current pointer."""
    if snapshot_info is None:
        return None
    current = _moid(getattr(snapshot_info, "currentSnapshot", None))
    tree = [
        map_snapshot_tree(node, current)
        for node in getattr(snapshot_info, "rootSnapshotList", None) or []
    ]
    return {"current_snapshot_moid": current, "count": _count_snapshots(tree), "tree": tree}


def map_snapshot_tree(node: Any, current_moid: str | None = None) -> dict[str, Any]:
    moid = _moid(getattr(node, "snapshot", None))
    return {
        "moid": moid,
        "id": getattr(node, "id", None),
        "name": getattr(node, "name", None),
        "description": getattr(node, "description", None),
        "created_at": as_timestamp(getattr(node, "createTime", None)),
        "power_state": as_text(getattr(node, "state", None)),
        "quiesced": getattr(node, "quiesced", None),
        "is_current": bool(current_moid) and moid == current_moid,
        "children": [
            map_snapshot_tree(child, current_moid)
            for child in getattr(node, "childSnapshotList", None) or []
        ],
    }


def _count_snapshots(tree: Sequence[dict[str, Any]]) -> int:
    return sum(1 + _count_snapshots(node["children"]) for node in tree)


def flatten_snapshots(tree: Sequence[dict[str, Any]], prefix: str = "") -> list[dict[str, Any]]:
    """Depth-first list of snapshots with a ``parent/child`` style path."""
    flattened: list[dict[str, Any]] = []
    for node in tree:
        path = f"{prefix}/{node['name']}" if prefix else str(node["name"])
        flattened.append({**{k: v for k, v in node.items() if k != "children"}, "path": path})
        flattened.extend(flatten_snapshots(node["children"], path))
    return flattened


# --------------------------------------------------------------------------- #
# Hosts
# --------------------------------------------------------------------------- #

HOST_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "overallStatus",
    "runtime.connectionState",
    "runtime.powerState",
    "runtime.inMaintenanceMode",
    "runtime.bootTime",
    "summary.hardware.vendor",
    "summary.hardware.model",
    "summary.hardware.cpuModel",
    "summary.hardware.numCpuPkgs",
    "summary.hardware.numCpuCores",
    "summary.hardware.numCpuThreads",
    "summary.hardware.cpuMhz",
    "summary.hardware.memorySize",
    "summary.hardware.numNics",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.overallMemoryUsage",
    "summary.quickStats.uptime",
    "summary.config.product.fullName",
    "summary.config.product.version",
    "summary.config.product.build",
    "summary.rebootRequired",
)

HOST_DETAIL_PROPERTIES: tuple[str, ...] = HOST_PROPERTIES + (
    "vm",
    "datastore",
    "network",
    "config.network.dnsConfig.hostName",
    "config.network.dnsConfig.domainName",
    "hardware.systemInfo.uuid",
    "hardware.biosInfo.biosVersion",
)


def map_host(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    parent_moid = _moid(record.props.get("parent"))
    cores = record.get("summary.hardware.numCpuCores")
    mhz = record.get("summary.hardware.cpuMhz")
    total_cpu_mhz = cores * mhz if cores and mhz else None
    memory_bytes = record.get("summary.hardware.memorySize")
    memory_used_mb = record.get("summary.quickStats.overallMemoryUsage")
    cpu_used_mhz = record.get("summary.quickStats.overallCpuUsage")
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "path": index.path_of(parent_moid, record.get("name")) if index else None,
        "datacenter": index.datacenter_of(parent_moid) if index else None,
        "cluster": index.name_of(parent_moid) if index else None,
        "connection_state": as_text(record.get("runtime.connectionState")),
        "power_state": as_text(record.get("runtime.powerState")),
        "in_maintenance_mode": record.get("runtime.inMaintenanceMode"),
        "overall_status": as_text(record.get("overallStatus")),
        "reboot_required": record.get("summary.rebootRequired"),
        "vendor": record.get("summary.hardware.vendor"),
        "model": record.get("summary.hardware.model"),
        "cpu_model": record.get("summary.hardware.cpuModel"),
        "cpu_sockets": record.get("summary.hardware.numCpuPkgs"),
        "cpu_cores": cores,
        "cpu_threads": record.get("summary.hardware.numCpuThreads"),
        "cpu_mhz_per_core": mhz,
        "cpu_total_mhz": total_cpu_mhz,
        "cpu_used_mhz": cpu_used_mhz,
        "cpu_used_percent": percent(cpu_used_mhz, total_cpu_mhz),
        "memory_gib": gib(memory_bytes),
        "memory_used_gib": gib(memory_used_mb, 1024),
        "memory_used_percent": percent(
            memory_used_mb, memory_bytes / (1024**2) if memory_bytes else None
        ),
        "nic_count": record.get("summary.hardware.numNics"),
        "esxi_version": record.get("summary.config.product.version"),
        "esxi_build": record.get("summary.config.product.build"),
        "product": record.get("summary.config.product.fullName"),
        "boot_time": as_timestamp(record.get("runtime.bootTime")),
        "uptime_seconds": record.get("summary.quickStats.uptime"),
    }


def map_host_detail(
    record: ObjectRecord, index: InventoryPathIndex | None = None
) -> dict[str, Any]:
    detail = map_host(record, index)
    detail.update(
        {
            "hostname": record.get("config.network.dnsConfig.hostName"),
            "domain": record.get("config.network.dnsConfig.domainName"),
            "hardware_uuid": record.get("hardware.systemInfo.uuid"),
            "bios_version": record.get("hardware.biosInfo.biosVersion"),
            "vm_count": len(record.get("vm", []) or []),
            "vm_moids": [_moid(vm) for vm in record.get("vm", []) or []],
            "datastore_moids": [_moid(ds) for ds in record.get("datastore", []) or []],
            "network_moids": [_moid(net) for net in record.get("network", []) or []],
        }
    )
    return detail


# --------------------------------------------------------------------------- #
# Clusters, resource pools, datacenters
# --------------------------------------------------------------------------- #

CLUSTER_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "overallStatus",
    "summary.numHosts",
    "summary.numEffectiveHosts",
    "summary.totalCpu",
    "summary.totalMemory",
    "summary.effectiveCpu",
    "summary.effectiveMemory",
    "summary.numCpuCores",
    "summary.numCpuThreads",
    "summary.currentFailoverLevel",
    "configuration.drsConfig.enabled",
    "configuration.drsConfig.defaultVmBehavior",
    "configuration.dasConfig.enabled",
    "configuration.dasConfig.admissionControlEnabled",
)


def map_cluster(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    parent_moid = _moid(record.props.get("parent"))
    total_memory = record.get("summary.totalMemory")
    effective_memory_mb = record.get("summary.effectiveMemory")
    total_cpu = record.get("summary.totalCpu")
    effective_cpu = record.get("summary.effectiveCpu")
    used_cpu = None if total_cpu is None or effective_cpu is None else total_cpu - effective_cpu
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "path": index.path_of(parent_moid, record.get("name")) if index else None,
        "datacenter": index.datacenter_of(parent_moid) if index else None,
        "overall_status": as_text(record.get("overallStatus")),
        "host_count": record.get("summary.numHosts"),
        "effective_host_count": record.get("summary.numEffectiveHosts"),
        "cpu_cores": record.get("summary.numCpuCores"),
        "cpu_threads": record.get("summary.numCpuThreads"),
        "cpu_total_mhz": total_cpu,
        "cpu_available_mhz": effective_cpu,
        "cpu_used_percent": percent(used_cpu, total_cpu),
        "memory_total_gib": gib(total_memory),
        "memory_available_gib": gib(effective_memory_mb, 1024),
        "drs_enabled": record.get("configuration.drsConfig.enabled"),
        "drs_behavior": as_text(record.get("configuration.drsConfig.defaultVmBehavior")),
        "ha_enabled": record.get("configuration.dasConfig.enabled"),
        "ha_admission_control": record.get("configuration.dasConfig.admissionControlEnabled"),
        "current_failover_level": record.get("summary.currentFailoverLevel"),
    }


RESOURCE_POOL_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "owner",
    "overallStatus",
    "runtime.cpu.reservationUsed",
    "runtime.cpu.maxUsage",
    "runtime.cpu.overallUsage",
    "runtime.memory.reservationUsed",
    "runtime.memory.maxUsage",
    "runtime.memory.overallUsage",
    "config.cpuAllocation",
    "config.memoryAllocation",
    "vm",
)


def map_resource_pool(
    record: ObjectRecord, index: InventoryPathIndex | None = None
) -> dict[str, Any]:
    parent_moid = _moid(record.props.get("parent"))
    owner_moid = _moid(record.props.get("owner"))
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "path": index.path_of(parent_moid, record.get("name")) if index else None,
        "owner": index.name_of(owner_moid) if index else None,
        "owner_moid": owner_moid,
        "overall_status": as_text(record.get("overallStatus")),
        "vm_count": len(record.get("vm", []) or []),
        "cpu_usage_mhz": record.get("runtime.cpu.overallUsage"),
        "cpu_max_mhz": record.get("runtime.cpu.maxUsage"),
        "cpu_reserved_mhz": record.get("runtime.cpu.reservationUsed"),
        "memory_usage_gib": gib(record.get("runtime.memory.overallUsage")),
        "memory_max_gib": gib(record.get("runtime.memory.maxUsage")),
        "cpu_allocation": map_resource_allocation(record.props.get("config.cpuAllocation")),
        "memory_allocation": map_resource_allocation(record.props.get("config.memoryAllocation")),
    }


DATACENTER_PROPERTIES: tuple[str, ...] = ("name", "parent")


def map_datacenter(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    parent_moid = _moid(record.props.get("parent"))
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "path": index.path_of(parent_moid, record.get("name")) if index else None,
    }


# --------------------------------------------------------------------------- #
# Storage and networking
# --------------------------------------------------------------------------- #

DATASTORE_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "overallStatus",
    "summary.type",
    "summary.capacity",
    "summary.freeSpace",
    "summary.uncommitted",
    "summary.accessible",
    "summary.maintenanceMode",
    "summary.multipleHostAccess",
    "summary.url",
    "host",
    "vm",
)


def map_datastore(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    capacity = record.get("summary.capacity")
    free = record.get("summary.freeSpace")
    uncommitted = record.get("summary.uncommitted")
    used = None if capacity is None or free is None else capacity - free
    provisioned = None if used is None else used + (uncommitted or 0)
    parent_moid = _moid(record.props.get("parent"))
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "datacenter": index.datacenter_of(parent_moid) if index else None,
        "type": record.get("summary.type"),
        "accessible": record.get("summary.accessible"),
        "maintenance_mode": as_text(record.get("summary.maintenanceMode")),
        "overall_status": as_text(record.get("overallStatus")),
        "capacity_gib": gib(capacity),
        "free_gib": gib(free),
        "used_gib": gib(used),
        "used_percent": percent(used, capacity),
        "provisioned_gib": gib(provisioned),
        "overprovisioned": None if provisioned is None or not capacity else provisioned > capacity,
        "shared": record.get("summary.multipleHostAccess"),
        "url": record.get("summary.url"),
        "host_count": len(record.get("host", []) or []),
        "vm_count": len(record.get("vm", []) or []),
    }


NETWORK_PROPERTIES: tuple[str, ...] = (
    "name",
    "parent",
    "overallStatus",
    "summary.accessible",
    "vm",
)

DVPORTGROUP_PROPERTIES: tuple[str, ...] = (
    "name",
    "config.numPorts",
    "config.type",
    "config.distributedVirtualSwitch",
    "config.defaultPortConfig",
)


def map_network(record: ObjectRecord, index: InventoryPathIndex | None = None) -> dict[str, Any]:
    parent_moid = _moid(record.props.get("parent"))
    return {
        "moid": record.moid,
        "name": record.get("name"),
        "type": record.type,
        "kind": _network_kind(record.type),
        "datacenter": index.datacenter_of(parent_moid) if index else None,
        "accessible": record.get("summary.accessible"),
        "overall_status": as_text(record.get("overallStatus")),
        "vm_count": len(record.get("vm", []) or []),
    }


def _network_kind(type_name: str) -> str:
    if "DistributedVirtualPortgroup" in type_name:
        return "distributed-portgroup"
    if "OpaqueNetwork" in type_name:
        return "opaque-network"
    return "standard-portgroup"


def map_dvportgroup_extras(record: ObjectRecord) -> dict[str, Any]:
    """Distributed-portgroup specifics, merged into the base network mapping."""
    default_config = record.props.get("config.defaultPortConfig")
    vlan = getattr(default_config, "vlan", None) if default_config is not None else None
    return {
        "port_count": record.get("config.numPorts"),
        "portgroup_type": as_text(record.get("config.type")),
        "dvs_moid": _moid(record.props.get("config.distributedVirtualSwitch")),
        "vlan": _map_vlan(vlan),
    }


def _map_vlan(vlan: Any) -> dict[str, Any] | None:
    if vlan is None:
        return None
    if isinstance(vlan, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
        return {
            "mode": "trunk",
            "ranges": [
                {"start": getattr(item, "start", None), "end": getattr(item, "end", None)}
                for item in getattr(vlan, "vlanId", None) or []
            ],
        }
    vlan_id = getattr(vlan, "vlanId", None)
    if vlan_id is None:
        return None
    return {"mode": "access", "vlan_id": vlan_id}


# --------------------------------------------------------------------------- #
# Tasks, events and alarms
# --------------------------------------------------------------------------- #


def map_task_info(info: Any, entity_name: str | None = None) -> dict[str, Any]:
    error = getattr(info, "error", None)
    return {
        "task_id": getattr(info, "key", None),
        "operation": getattr(info, "descriptionId", None),
        "state": as_text(getattr(info, "state", None)),
        "progress_percent": getattr(info, "progress", None),
        "entity": entity_name or getattr(info, "entityName", None),
        "queued_at": as_timestamp(getattr(info, "queueTime", None)),
        "started_at": as_timestamp(getattr(info, "startTime", None)),
        "completed_at": as_timestamp(getattr(info, "completeTime", None)),
        "initiated_by": getattr(info, "reason", None)
        and getattr(getattr(info, "reason", None), "userName", None),
        "cancelable": getattr(info, "cancelable", None),
        "error": _describe_fault(error),
    }


def _describe_fault(fault: Any) -> str | None:
    if fault is None:
        return None
    message = getattr(fault, "msg", None) or getattr(fault, "localizedMessage", None)
    return message or f"{type(fault).__name__}"


def map_event(event: Any) -> dict[str, Any]:
    vm = getattr(event, "vm", None)
    host = getattr(event, "host", None)
    return {
        "key": getattr(event, "key", None),
        "type": type(event).__name__,
        "created_at": as_timestamp(getattr(event, "createdTime", None)),
        "user": getattr(event, "userName", None) or None,
        "message": (getattr(event, "fullFormattedMessage", None) or "").strip() or None,
        "vm": getattr(vm, "name", None) if vm is not None else None,
        "host": getattr(host, "name", None) if host is not None else None,
        "datacenter": getattr(getattr(event, "datacenter", None), "name", None),
    }


def map_triggered_alarm(alarm_state: Any, entity_name: str | None = None) -> dict[str, Any]:
    alarm = getattr(alarm_state, "alarm", None)
    entity = getattr(alarm_state, "entity", None)
    return {
        "key": getattr(alarm_state, "key", None),
        "alarm_moid": _moid(alarm),
        "entity": entity_name,
        "entity_moid": _moid(entity),
        "entity_type": type(entity).__name__ if entity is not None else None,
        "status": as_text(getattr(alarm_state, "overallStatus", None)),
        "acknowledged": getattr(alarm_state, "acknowledged", None),
        "acknowledged_by": getattr(alarm_state, "acknowledgedByUser", None),
        "triggered_at": as_timestamp(getattr(alarm_state, "time", None)),
    }


def map_about_info(about: Any) -> dict[str, Any]:
    return {
        "name": getattr(about, "fullName", None),
        "product": getattr(about, "name", None),
        "version": getattr(about, "version", None),
        "build": getattr(about, "build", None),
        "api_version": getattr(about, "apiVersion", None),
        "api_type": getattr(about, "apiType", None),
        "os_type": getattr(about, "osType", None),
        "vendor": getattr(about, "vendor", None),
        "instance_uuid": getattr(about, "instanceUuid", None),
        "license_product": getattr(about, "licenseProductName", None),
    }


def map_performance_series(
    counter_name: str, unit: str, values: Sequence[int | float]
) -> dict[str, Any]:
    """Summarise one performance counter series."""
    numeric = [value for value in values if value is not None and value >= 0]
    return {
        "counter": counter_name,
        "unit": unit,
        "samples": len(numeric),
        "latest": numeric[-1] if numeric else None,
        "average": round(sum(numeric) / len(numeric), 2) if numeric else None,
        "minimum": min(numeric) if numeric else None,
        "maximum": max(numeric) if numeric else None,
    }
