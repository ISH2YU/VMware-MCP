"""An in-memory stand-in for a vCenter Server.

The fake implements the two seams the server actually depends on: the SOAP stub
(``InvokeMethod``/``InvokeAccessor``, which is how pyVmomi turns ``vm.PowerOn()``
into a wire call) and the PropertyCollector. Everything above those seams -- the
real ``VSphereClient``, the real property specs, the real mappers and the real
tools -- runs unmodified against it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, ClassVar

from pyVmomi import vim

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@dataclass
class Invocation:
    """One recorded managed-object method call."""

    moid: str
    method: str
    args: dict[str, Any]


@dataclass
class FakeEntity:
    moid: str
    vim_type: type
    props: dict[str, Any] = field(default_factory=dict)


class FakeStub:
    """Stands in for pyVmomi's SOAP stub adapter."""

    def __init__(self, inventory: FakeInventory) -> None:
        self.inventory = inventory
        self.invocations: list[Invocation] = []

    def InvokeMethod(self, mo: Any, info: Any, args: Any) -> Any:  # noqa: N802 - pyVmomi API
        # pyVmomi hands over a positional list aligned with the method's params,
        # and names methods by their short form ("PowerOff"); the wsdl name
        # ("PowerOffVM_Task") is the one vSphere operators recognise.
        arguments = dict(zip((param.name for param in info.params), args or [], strict=False))
        self.invocations.append(Invocation(moid=mo._moId, method=info.wsdlName, args=arguments))
        return self.inventory.invoke(mo, info, arguments)

    def InvokeAccessor(self, mo: Any, info: Any) -> Any:  # noqa: N802 - pyVmomi API
        return self.inventory.read_property(mo, info.name)

    def methods(self, name: str) -> list[Invocation]:
        return [call for call in self.invocations if call.method == name]


class FakeInventory:
    """Holds entities and answers property and method calls about them."""

    def __init__(self, entities: Iterable[FakeEntity] | None = None) -> None:
        self.entities: list[FakeEntity] = list(entities or [])
        self.stub = FakeStub(self)
        self.tasks: dict[str, Any] = {}
        self.task_state = "success"
        self._task_counter = 0
        self.register_task("task-recent", "running", "VirtualMachine.clone", progress=42)

    # -- construction ------------------------------------------------------ #

    def add(self, moid: str, vim_type: type, **props: Any) -> FakeEntity:
        entity = FakeEntity(moid=moid, vim_type=vim_type, props=props)
        self.entities.append(entity)
        return entity

    def ref(self, moid: str) -> Any:
        """A real pyVmomi managed object reference bound to the fake stub."""
        entity = self.get(moid)
        return entity.vim_type(moid, self.stub)

    def get(self, moid: str) -> FakeEntity:
        for entity in self.entities:
            if entity.moid == moid:
                return entity
        raise KeyError(moid)

    def of_type(self, vim_type: type) -> list[FakeEntity]:
        return [entity for entity in self.entities if issubclass(entity.vim_type, vim_type)]

    # -- stub behaviour ---------------------------------------------------- #

    def invoke(self, mo: Any, info: Any, args: dict[str, Any]) -> Any:
        if info.result is vim.Task:
            return self.new_task(info.wsdlName)
        return None

    def register_task(self, moid: str, state: str, operation: str, *, progress: int = 100) -> None:
        self.tasks[moid] = SimpleNamespace(
            key=moid,
            descriptionId=operation,
            state=state,
            progress=progress,
            entityName=None,
            queueTime=NOW,
            startTime=NOW,
            completeTime=NOW + timedelta(seconds=2) if state == "success" else None,
            reason=SimpleNamespace(userName="tester"),
            cancelable=False,
            error=SimpleNamespace(msg="it broke") if state == "error" else None,
        )

    def new_task(self, operation: str) -> Any:
        self._task_counter += 1
        moid = f"task-{self._task_counter}"
        self.register_task(moid, self.task_state, operation)
        return vim.Task(moid, self.stub)

    def read_property(self, mo: Any, name: str) -> Any:
        if isinstance(mo, vim.Task) and name == "info":
            return self.tasks[mo._moId]
        try:
            return self.get(mo._moId).props.get(name)
        except KeyError:
            return None

    # -- property collector ------------------------------------------------ #

    def retrieve(self, spec_set: list[Any]) -> Any:
        results = []
        for filter_spec in spec_set:
            prop_spec = filter_spec.propSet[0]
            paths = list(prop_spec.pathSet or [])
            object_specs = list(filter_spec.objectSet or [])
            container_mode = len(object_specs) == 1 and bool(object_specs[0].selectSet)
            if container_mode:
                candidates = self.of_type(prop_spec.type)
            else:
                wanted = {spec.obj._moId for spec in object_specs}
                candidates = [entity for entity in self.entities if entity.moid in wanted]
            for entity in candidates:
                prop_set = [
                    SimpleNamespace(name=path, val=entity.props[path])
                    for path in paths
                    if path in entity.props
                ]
                results.append(
                    SimpleNamespace(obj=self.ref(entity.moid), propSet=prop_set, missingSet=[])
                )
        return SimpleNamespace(objects=results, token=None)


class FakeHistoryCollector:
    """Mimics an event/task history collector's ``latestPage`` behaviour."""

    def __init__(self, page: list[Any]) -> None:
        self._page = page
        self.page_size = 1000
        self.destroyed = False

    def SetCollectorPageSize(self, size: int) -> None:  # noqa: N802 - pyVmomi API
        self.page_size = size

    @property
    def latestPage(self) -> list[Any]:  # noqa: N802 - pyVmomi API
        # vCenter returns the newest entries oldest-first within the page.
        return self._page[-self.page_size :]

    def DestroyCollector(self) -> None:  # noqa: N802 - pyVmomi API
        self.destroyed = True


def _counter(key: int, group: str, name: str, rollup: str, unit: str) -> Any:
    return SimpleNamespace(
        key=key,
        groupInfo=SimpleNamespace(key=group),
        nameInfo=SimpleNamespace(key=name),
        rollupType=rollup,
        unitInfo=SimpleNamespace(key=unit),
    )


class FakePerformanceManager:
    """Serves a handful of counters with deterministic samples."""

    COUNTERS = (
        _counter(1, "cpu", "usage", "average", "percent"),
        _counter(2, "cpu", "usagemhz", "average", "megaHertz"),
        _counter(3, "mem", "usage", "average", "percent"),
        _counter(12, "mem", "consumed", "average", "kiloBytes"),
        _counter(4, "cpu", "ready", "summation", "millisecond"),
        _counter(5, "mem", "swapinRate", "average", "kiloBytesPerSecond"),
        _counter(6, "disk", "usage", "average", "kiloBytesPerSecond"),
        _counter(7, "net", "usage", "average", "kiloBytesPerSecond"),
        _counter(8, "disk", "maxTotalLatency", "latest", "millisecond"),
    )

    #: Raw counter values keyed by counter id, in vSphere's native units.
    SAMPLES: ClassVar[dict[int, list[int]]] = {
        1: [1000, 2500, 1500],
        2: [400, 900, 600],
        3: [4000, 4200, 4400],
    }

    def __init__(self, current_supported: bool = True) -> None:
        self.perfCounter = list(self.COUNTERS)
        self.current_supported = current_supported
        self.queries: list[Any] = []

    def QueryPerfProviderSummary(self, entity: Any) -> Any:  # noqa: N802 - pyVmomi API
        return SimpleNamespace(currentSupported=self.current_supported, refreshRate=20)

    def QueryPerf(self, querySpec: list[Any]) -> list[Any]:  # noqa: N802, N803 - pyVmomi API
        self.queries.extend(querySpec)
        spec = querySpec[0]
        values = [
            SimpleNamespace(
                id=SimpleNamespace(counterId=metric.counterId, instance=""),
                value=self.SAMPLES.get(metric.counterId, [0, 0, 0]),
            )
            for metric in spec.metricId
        ]
        sample_info = [
            SimpleNamespace(timestamp=NOW - timedelta(seconds=40), interval=20),
            SimpleNamespace(timestamp=NOW - timedelta(seconds=20), interval=20),
            SimpleNamespace(timestamp=NOW, interval=20),
        ]
        return [SimpleNamespace(entity=spec.entity, sampleInfo=sample_info, value=values)]


def _port_policy(vlan_id: int) -> Any:
    return vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
        vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(vlanId=vlan_id)
    )


def _event(event_type: type, key: int, message: str, minutes_ago: int, user: str = "admin") -> Any:
    return event_type(
        key=key,
        chainId=key,
        createdTime=NOW - timedelta(minutes=minutes_ago),
        userName=user,
        fullFormattedMessage=message,
        vm=vim.event.VmEventArgument(name="web-01", vm=vim.VirtualMachine("vm-101", None)),
    )


def build_events() -> list[Any]:
    """Oldest first, matching how vCenter fills a collector page."""
    return [
        _event(vim.event.VmPoweredOffEvent, 1, "web-01 powered off", 120),
        _event(vim.event.VmReconfiguredEvent, 2, "web-01 reconfigured", 60),
        _event(vim.event.VmPoweredOnEvent, 3, "web-01 powered on", 10),
    ]


def build_tasks() -> list[Any]:
    return [
        vim.TaskInfo(
            key=f"task-{index}",
            task=vim.Task(f"task-{index}", None),
            descriptionId=description,
            state=state,
            entityName="web-01",
            progress=100,
            cancelable=False,
            queueTime=NOW - timedelta(minutes=index * 10),
            startTime=NOW - timedelta(minutes=index * 10),
            completeTime=NOW - timedelta(minutes=index * 10 - 1),
            reason=vim.TaskReasonUser(userName="admin"),
        )
        for index, (description, state) in enumerate(
            [
                ("VirtualMachine.reconfigure", vim.TaskInfo.State.success),
                ("VirtualMachine.powerOn", vim.TaskInfo.State.error),
            ],
            start=1,
        )
    ]


class FakeServiceInstance:
    """The subset of ``vim.ServiceInstance`` the server touches."""

    def __init__(self, inventory: FakeInventory) -> None:
        self.inventory = inventory
        self._stub = inventory.stub
        self.performance_manager = FakePerformanceManager()
        self.events = build_events()
        self.tasks = build_tasks()
        self.event_filters: list[Any] = []
        self.task_filters: list[Any] = []
        self._content = SimpleNamespace(
            rootFolder=vim.Folder("group-d1", inventory.stub),
            viewManager=SimpleNamespace(CreateContainerView=self._create_view),
            propertyCollector=SimpleNamespace(
                RetrievePropertiesEx=lambda specSet, options: inventory.retrieve(  # noqa: N803
                    specSet
                ),
                ContinueRetrievePropertiesEx=lambda token: None,
            ),
            about=SimpleNamespace(
                fullName="VMware vCenter Server 8.0.3 build-24022515",
                name="VMware vCenter Server",
                version="8.0.3",
                build="24022515",
                apiVersion="8.0.3.0",
                apiType="VirtualCenter",
                osType="linux-x64",
                vendor="VMware, Inc.",
                instanceUuid="aaaa-bbbb-cccc",
                licenseProductName="VMware VirtualCenter Server",
            ),
            sessionManager=SimpleNamespace(
                currentSession=SimpleNamespace(userName="svc-mcp@vsphere.local", locale="en")
            ),
            perfManager=self.performance_manager,
            eventManager=SimpleNamespace(CreateCollectorForEvents=self._event_collector),
            taskManager=SimpleNamespace(
                CreateCollectorForTasks=self._task_collector,
                recentTask=[vim.Task("task-recent", inventory.stub)],
            ),
        )

    def _event_collector(self, filter: Any) -> FakeHistoryCollector:  # noqa: A002 - pyVmomi API
        self.event_filters.append(filter)
        return FakeHistoryCollector(self.events)

    def _task_collector(self, filter: Any) -> FakeHistoryCollector:  # noqa: A002 - pyVmomi API
        self.task_filters.append(filter)
        return FakeHistoryCollector(self.tasks)

    def _create_view(self, container: Any, types: list[type], recursive: bool) -> Any:
        return vim.view.ContainerView("session-view-1", self.inventory.stub)

    def RetrieveContent(self) -> Any:  # noqa: N802 - pyVmomi API
        return self._content

    def CurrentTime(self) -> datetime:  # noqa: N802 - pyVmomi API
        return NOW


class FakeSession:
    """Drop-in replacement for :class:`~vmware_mcp.vsphere.session.VSphereSession`."""

    def __init__(self, inventory: FakeInventory) -> None:
        self.inventory = inventory
        self.instance = FakeServiceInstance(inventory)
        self.closed = False

    def service_instance(self) -> Any:
        return self.instance

    def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(self.instance, *args, **kwargs)

    def close(self) -> None:
        self.closed = True


def build_inventory() -> FakeInventory:
    """A small but structurally realistic vCenter: one datacenter, one cluster."""
    inventory = FakeInventory()
    stub = inventory.stub

    def ref(moid: str, vim_type: type) -> Any:
        return vim_type(moid, stub)

    root = ref("group-d1", vim.Folder)
    inventory.add("group-d1", vim.Folder, name="Datacenters", parent=None)
    inventory.add("datacenter-2", vim.Datacenter, name="DC1", parent=root)
    datacenter = ref("datacenter-2", vim.Datacenter)
    inventory.add("group-v3", vim.Folder, name="vm", parent=datacenter)
    inventory.add("group-h4", vim.Folder, name="host", parent=datacenter)
    vm_folder = ref("group-v3", vim.Folder)
    host_folder = ref("group-h4", vim.Folder)
    inventory.add("group-v9", vim.Folder, name="Tier1", parent=vm_folder)
    tier1 = ref("group-v9", vim.Folder)

    inventory.add(
        "domain-c7",
        vim.ClusterComputeResource,
        name="Cluster-A",
        parent=host_folder,
        overallStatus=vim.ManagedEntity.Status.green,
        **{
            "summary.numHosts": 2,
            "summary.numEffectiveHosts": 2,
            "summary.totalCpu": 96000,
            "summary.effectiveCpu": 72000,
            "summary.totalMemory": 549755813888,
            "summary.effectiveMemory": 393216,
            "summary.numCpuCores": 48,
            "summary.numCpuThreads": 96,
            "configuration.drsConfig.enabled": True,
            "configuration.drsConfig.defaultVmBehavior": "fullyAutomated",
            "configuration.dasConfig.enabled": True,
            "configuration.dasConfig.admissionControlEnabled": True,
        },
    )
    cluster = ref("domain-c7", vim.ClusterComputeResource)
    inventory.add(
        "resgroup-8",
        vim.ResourcePool,
        name="Resources",
        parent=cluster,
        owner=cluster,
        **{
            "runtime.cpu.overallUsage": 12000,
            "runtime.cpu.maxUsage": 96000,
            "runtime.memory.overallUsage": 137438953472,
            "runtime.memory.maxUsage": 549755813888,
            "vm": [ref("vm-101", vim.VirtualMachine)],
        },
    )
    inventory.get("domain-c7").props["resourcePool"] = ref("resgroup-8", vim.ResourcePool)

    for moid, host_name, cpu_usage, memory_usage in (
        ("host-11", "esxi-01.lab.local", 8000, 131072),
        ("host-12", "esxi-02.lab.local", 4000, 65536),
    ):
        inventory.add(
            moid,
            vim.HostSystem,
            name=host_name,
            parent=cluster,
            overallStatus=vim.ManagedEntity.Status.green,
            **{
                "runtime.connectionState": vim.HostSystem.ConnectionState.connected,
                "runtime.powerState": vim.HostSystem.PowerState.poweredOn,
                "runtime.inMaintenanceMode": False,
                "runtime.bootTime": NOW - timedelta(days=30),
                "summary.hardware.vendor": "Dell Inc.",
                "summary.hardware.model": "PowerEdge R650",
                "summary.hardware.cpuModel": "Intel(R) Xeon(R) Gold 6338",
                "summary.hardware.numCpuPkgs": 2,
                "summary.hardware.numCpuCores": 24,
                "summary.hardware.numCpuThreads": 48,
                "summary.hardware.cpuMhz": 2000,
                "summary.hardware.memorySize": 274877906944,
                "summary.hardware.numNics": 4,
                "summary.quickStats.overallCpuUsage": cpu_usage,
                "summary.quickStats.overallMemoryUsage": memory_usage,
                "summary.quickStats.uptime": 2592000,
                "summary.config.product.fullName": "VMware ESXi 8.0.3 build-24022510",
                "summary.config.product.version": "8.0.3",
                "summary.config.product.build": "24022510",
                "summary.rebootRequired": False,
                "vm": [],
                "datastore": [ref("datastore-21", vim.Datastore)],
                "network": [ref("network-31", vim.Network)],
                "hardware.systemInfo.uuid": f"uuid-{moid}",
                "hardware.biosInfo.biosVersion": "1.9.2",
                "config.network.dnsConfig.hostName": host_name.split(".")[0],
                "config.network.dnsConfig.domainName": "lab.local",
            },
        )

    def add_vm(
        moid: str,
        name: str,
        host_moid: str,
        power_state: str,
        *,
        parent: Any = tier1,
        template: bool = False,
        ip: str | None = None,
        guest_os: str = "Ubuntu Linux (64-bit)",
        cpus: int = 4,
        memory_mb: int = 8192,
        tools: str = "guestToolsRunning",
    ) -> None:
        inventory.add(
            moid,
            vim.VirtualMachine,
            name=name,
            parent=parent,
            resourcePool=ref("resgroup-8", vim.ResourcePool),
            overallStatus=vim.ManagedEntity.Status.green,
            **{
                "config.uuid": f"4213{moid}-bios",
                "config.instanceUuid": f"5013{moid}-instance",
                "config.template": template,
                "config.guestFullName": guest_os,
                "config.hardware.numCPU": cpus,
                "config.hardware.memoryMB": memory_mb,
                "config.annotation": f"notes for {name}",
                "config.version": "vmx-19",
                "config.files.vmPathName": f"[ds-nvme] {name}/{name}.vmx",
                "runtime.powerState": power_state,
                "runtime.connectionState": vim.VirtualMachine.ConnectionState.connected,
                "runtime.host": ref(host_moid, vim.HostSystem),
                "runtime.bootTime": NOW - timedelta(days=3),
                "guest.hostName": f"{name}.lab.local",
                "guest.ipAddress": ip,
                "guest.guestState": "running" if power_state == "poweredOn" else "notRunning",
                "guest.toolsStatus": "toolsOk",
                "guest.toolsRunningStatus": tools,
                "summary.quickStats.overallCpuUsage": 450,
                "summary.quickStats.guestMemoryUsage": 3072,
                "summary.quickStats.uptimeSeconds": 259200,
                "summary.storage.committed": 64424509440,
                "summary.storage.uncommitted": 10737418240,
                "datastore": [ref("datastore-21", vim.Datastore)],
                "network": [ref("network-31", vim.Network)],
            },
        )

    add_vm("vm-101", "web-01", "host-11", "poweredOn", ip="10.0.0.11")
    add_vm(
        "vm-102",
        "db-01",
        "host-12",
        "poweredOff",
        guest_os="Red Hat Enterprise Linux 9 (64-bit)",
        cpus=8,
        memory_mb=32768,
        tools="guestToolsNotRunning",
    )
    add_vm(
        "vm-103",
        "ubuntu-2404-template",
        "host-11",
        "poweredOff",
        parent=ref("group-v3", vim.Folder),
        template=True,
        tools="guestToolsNotRunning",
    )
    inventory.get("host-11").props["vm"] = [
        ref("vm-101", vim.VirtualMachine),
        ref("vm-103", vim.VirtualMachine),
    ]
    inventory.get("host-12").props["vm"] = [ref("vm-102", vim.VirtualMachine)]

    for moid, name, capacity, free, kind in (
        ("datastore-21", "ds-nvme", 4398046511104, 1099511627776, "VMFS"),
        ("datastore-22", "ds-nfs", 2199023255552, 219902325555, "NFS"),
    ):
        inventory.add(
            moid,
            vim.Datastore,
            name=name,
            parent=datacenter,
            overallStatus=vim.ManagedEntity.Status.green,
            **{
                "summary.type": kind,
                "summary.capacity": capacity,
                "summary.freeSpace": free,
                "summary.uncommitted": 549755813888,
                "summary.accessible": True,
                "summary.maintenanceMode": "normal",
                "summary.multipleHostAccess": True,
                "summary.url": f"ds:///vmfs/volumes/{moid}/",
                "host": [ref("host-11", vim.HostSystem)],
                "vm": [ref("vm-101", vim.VirtualMachine)],
            },
        )

    inventory.add(
        "network-31",
        vim.Network,
        name="VM Network",
        parent=datacenter,
        overallStatus=vim.ManagedEntity.Status.green,
        **{"summary.accessible": True, "vm": [ref("vm-101", vim.VirtualMachine)]},
    )
    inventory.add(
        "dvportgroup-32",
        vim.dvs.DistributedVirtualPortgroup,
        name="dvpg-prod",
        parent=datacenter,
        overallStatus=vim.ManagedEntity.Status.green,
        **{
            "summary.accessible": True,
            "vm": [],
            "config.numPorts": 128,
            "config.type": "earlyBinding",
            "config.distributedVirtualSwitch": ref("dvs-30", vim.DistributedVirtualSwitch),
            "config.defaultPortConfig": _port_policy(vlan_id=120),
        },
    )
    inventory.add("dvs-30", vim.DistributedVirtualSwitch, name="DSwitch", parent=datacenter)

    inventory.add(
        "alarm-17",
        vim.alarm.Alarm,
        **{"info.name": "Host memory usage", "info.description": "Memory usage is high"},
    )
    inventory.get("host-11").props["triggeredAlarmState"] = [
        vim.alarm.AlarmState(
            key="alarm-17.host-11",
            entity=ref("host-11", vim.HostSystem),
            alarm=ref("alarm-17", vim.alarm.Alarm),
            overallStatus=vim.ManagedEntity.Status.red,
            time=NOW - timedelta(hours=2),
            acknowledged=False,
        )
    ]
    inventory.get("datastore-22").props["triggeredAlarmState"] = [
        vim.alarm.AlarmState(
            key="alarm-17.datastore-22",
            entity=ref("datastore-22", vim.Datastore),
            alarm=ref("alarm-17", vim.alarm.Alarm),
            overallStatus=vim.ManagedEntity.Status.yellow,
            time=NOW - timedelta(hours=1),
            acknowledged=True,
        )
    ]

    inventory.get("vm-101").props["snapshot"] = snapshot_info()
    return inventory


def snapshot_info() -> Any:
    """A two-level snapshot tree with the child as the current snapshot."""
    child = vim.vm.SnapshotTree(
        snapshot=vim.vm.Snapshot("snapshot-3", None),
        vm=vim.VirtualMachine("vm-101", None),
        name="after-patch",
        description="",
        id=3,
        createTime=NOW - timedelta(days=2),
        state=vim.VirtualMachinePowerState.poweredOn,
        quiesced=False,
    )
    root = vim.vm.SnapshotTree(
        snapshot=vim.vm.Snapshot("snapshot-2", None),
        vm=vim.VirtualMachine("vm-101", None),
        name="before-patch",
        description="taken before the March update",
        id=2,
        createTime=NOW - timedelta(days=7),
        state=vim.VirtualMachinePowerState.poweredOn,
        quiesced=True,
        childSnapshotList=[child],
    )
    return vim.vm.SnapshotInfo(
        currentSnapshot=vim.vm.Snapshot("snapshot-3", None), rootSnapshotList=[root]
    )
