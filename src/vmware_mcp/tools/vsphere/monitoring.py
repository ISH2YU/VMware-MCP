"""Tasks, events, alarms and performance metrics."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...errors import InvalidArgumentError
from ...vsphere import lookup, mappers, monitoring, perf
from ...vsphere.tasks import read_task_info
from .._common import ToolContext, mcp_tool, paginate

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

EntityType = Literal["vm", "host", "cluster", "datastore", "datacenter"]
Interval = Literal["realtime", "5min", "30min", "2hours", "1day"]

_ENTITY_KINDS = {
    "vm": lookup.VM,
    "host": lookup.HOST,
    "cluster": lookup.CLUSTER,
    "datastore": lookup.DATASTORE,
    "datacenter": lookup.DATACENTER,
}


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    async def _entity_moid(entity_type: str | None, entity: str | None) -> tuple[Any, str | None]:
        if entity is None:
            return None, None
        kind = _ENTITY_KINDS.get(entity_type or "vm")
        if kind is None:
            raise InvalidArgumentError(
                f"Unsupported entity_type {entity_type!r}. "
                f"Expected one of: {', '.join(_ENTITY_KINDS)}."
            )
        record = await client.resolve(kind, entity)
        return kind.vim_type, record.moid

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_tasks(
        entity: str | None = None,
        entity_type: EntityType = "vm",
        hours: float = 24,
        states: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List recent vSphere tasks, newest first.

        Use this to see what changed in the environment and whether an
        operation someone else started succeeded.

        Args:
            entity: Restrict to tasks on this object and its children.
            entity_type: What kind of object ``entity`` names.
            hours: How far back to look, in hours.
            states: Filter by task state: ``queued``, ``running``, ``success``
                or ``error``.
            limit: Maximum number of tasks to return.
        """
        vim_type, moid = await _entity_moid(entity_type, entity)
        max_count = min(limit or settings.default_page_size, settings.max_results)

        def work(service_instance: vim.ServiceInstance) -> list[dict[str, Any]]:
            target = monitoring.entity_reference(service_instance, moid, vim_type) if moid else None
            return monitoring.query_tasks(
                service_instance,
                entity=target,
                hours=hours,
                states=states,
                max_count=max_count,
            )

        tasks = await client.call(work)
        return {"count": len(tasks), "hours": hours, "tasks": tasks}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_get_task(task_id: str) -> dict[str, Any]:
        """Check the state and progress of a single vSphere task.

        Tools that started work with ``wait=false``, or that timed out waiting,
        return a ``task_id`` you can poll here.

        Args:
            task_id: The task's managed object id, e.g. ``task-4218``.
        """
        info = await client.call(read_task_info, task_id.strip())
        return {"task": mappers.map_task_info(info)}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_running_tasks() -> dict[str, Any]:
        """List the tasks vCenter is currently running or has just finished."""
        tasks = await client.call(monitoring.recent_task_summary)
        return {"count": len(tasks), "tasks": tasks}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_events(
        entity: str | None = None,
        entity_type: EntityType = "vm",
        hours: float = 24,
        categories: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List recent vSphere events, newest first.

        Events are the audit trail of the environment: logins, configuration
        changes, HA actions, alarm transitions and hardware problems.

        Args:
            entity: Restrict to events about this object and its children.
            entity_type: What kind of object ``entity`` names.
            hours: How far back to look, in hours.
            categories: Filter by ``info``, ``warning``, ``error`` or ``user``.
            limit: Maximum number of events to return.
        """
        if categories:
            unknown = [
                category
                for category in categories
                if category.lower() not in monitoring.EVENT_CATEGORIES
            ]
            if unknown:
                raise InvalidArgumentError(
                    f"Unknown event categories: {', '.join(unknown)}. "
                    f"Expected: {', '.join(monitoring.EVENT_CATEGORIES)}."
                )
        vim_type, moid = await _entity_moid(entity_type, entity)
        max_count = min(limit or settings.default_page_size, settings.max_results)

        def work(service_instance: vim.ServiceInstance) -> list[dict[str, Any]]:
            target = monitoring.entity_reference(service_instance, moid, vim_type) if moid else None
            return monitoring.query_events(
                service_instance,
                entity=target,
                hours=hours,
                categories=[category.lower() for category in categories] if categories else None,
                max_count=max_count,
            )

        events = await client.call(work)
        return {"count": len(events), "hours": hours, "events": events}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_alarms(
        status: str | None = None,
        include_acknowledged: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List every currently triggered alarm across the inventory.

        This is the fastest way to answer "what is unhealthy right now".

        Args:
            status: Filter by severity: ``red`` (alert) or ``yellow`` (warning).
            include_acknowledged: Include alarms an operator has already
                acknowledged.
            limit: Maximum number of alarms to return.
            offset: Number of matches to skip, for paging.
        """
        alarms = await client.call(monitoring.query_triggered_alarms)
        filtered = [
            alarm
            for alarm in alarms
            if (status is None or (alarm["status"] or "").lower() == status.lower())
            and (include_acknowledged or not alarm.get("acknowledged"))
        ]
        ordered = sorted(
            filtered,
            key=lambda alarm: (alarm["status"] != "red", alarm.get("triggered_at") or ""),
        )
        page, meta = paginate(ordered, limit=limit, offset=offset, settings=settings)
        red = sum(1 for alarm in filtered if alarm["status"] == "red")
        return {
            **meta,
            "red_count": red,
            "yellow_count": len(filtered) - red,
            "alarms": page,
        }

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_get_performance(
        entity: str,
        entity_type: Literal["vm", "host"] = "vm",
        interval: Interval = "realtime",
        samples: int = 15,
        counters: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read CPU, memory, disk and network performance counters for a VM or host.

        Each counter comes back summarised (latest, average, minimum, maximum)
        over the requested window. ``realtime`` gives 20-second samples for
        roughly the last hour; the rollup intervals cover longer periods.

        Args:
            entity: VM or host name, managed object id, UUID or inventory path.
            entity_type: Whether ``entity`` is a ``vm`` or a ``host``.
            interval: ``realtime``, ``5min``, ``30min``, ``2hours`` or ``1day``.
            samples: Number of samples to retrieve, most recent first.
            counters: Specific counters in ``group.name.rollup`` form, e.g.
                ``cpu.ready.summation``. Defaults to a standard set.
        """
        if samples < 1:
            raise InvalidArgumentError("samples must be at least 1.")
        kind = lookup.VM if entity_type == "vm" else lookup.HOST
        record = await client.resolve(kind, entity)
        moid = record.moid
        vim_type = kind.vim_type
        selected = (
            list(counters)
            if counters
            else list(perf.VM_COUNTERS if entity_type == "vm" else perf.HOST_COUNTERS)
        )

        def work(service_instance: vim.ServiceInstance) -> dict[str, Any]:
            target = monitoring.entity_reference(service_instance, moid, vim_type)
            return perf.query_metrics(
                service_instance,
                target,
                selected,
                max_samples=min(samples, 180),
                interval=interval,
            )

        metrics = await client.call(work)
        return {"entity": record.get("name"), "moid": moid, "type": entity_type, **metrics}
