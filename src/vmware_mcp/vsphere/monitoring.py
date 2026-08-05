"""Event, task and alarm queries.

vCenter exposes history through collector objects rather than plain queries; a
collector's ``latestPage`` is the cheapest way to get the most recent N entries
without dragging the whole history across the wire.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from pyVmomi import vim

from .mappers import map_event, map_task_info, map_triggered_alarm
from .query import collect_properties, collect_properties_for, moid_of, require_manager

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 1000

EVENT_CATEGORIES = ("info", "warning", "error", "user")


def _destroy(collector: Any) -> None:
    try:
        collector.DestroyCollector()
    except Exception:
        logger.debug("Failed to destroy history collector", exc_info=True)


def _since(hours: float | None) -> datetime | None:
    if hours is None:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=hours)


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _newest_first(items: list[Any], *timestamp_fields: str) -> list[Any]:
    """Sort by the first populated timestamp field, most recent first.

    The ordering of a collector's ``latestPage`` is not something to rely on,
    and entries can be missing a timestamp entirely, so sort explicitly.
    """

    def key(item: Any) -> datetime:
        for field in timestamp_fields:
            value = getattr(item, field, None)
            if isinstance(value, datetime):
                return value
        return _EPOCH

    return sorted(items, key=key, reverse=True)


def query_events(
    service_instance: vim.ServiceInstance,
    *,
    entity: Any = None,
    hours: float | None = 24,
    categories: Sequence[str] | None = None,
    max_count: int = 100,
) -> list[dict[str, Any]]:
    """Most recent events, newest first."""
    content = service_instance.RetrieveContent()
    spec = vim.event.EventFilterSpec()
    if entity is not None:
        spec.entity = vim.event.EventFilterSpec.ByEntity(
            entity=entity, recursion=vim.event.EventFilterSpec.RecursionOption.all
        )
    begin = _since(hours)
    if begin is not None:
        spec.time = vim.event.EventFilterSpec.ByTime(beginTime=begin)
    if categories:
        spec.category = list(categories)

    event_manager = require_manager(content.eventManager, "event manager")
    collector = event_manager.CreateCollectorForEvents(filter=spec)
    try:
        collector.SetCollectorPageSize(min(max_count, MAX_PAGE_SIZE))
        events = list(collector.latestPage or [])
    finally:
        _destroy(collector)
    return [map_event(event) for event in _newest_first(events, "createdTime")[:max_count]]


def query_tasks(
    service_instance: vim.ServiceInstance,
    *,
    entity: Any = None,
    hours: float | None = 24,
    states: Sequence[str] | None = None,
    max_count: int = 100,
) -> list[dict[str, Any]]:
    """Most recent tasks, newest first."""
    content = service_instance.RetrieveContent()
    spec = vim.TaskFilterSpec()
    if entity is not None:
        spec.entity = vim.TaskFilterSpec.ByEntity(
            entity=entity, recursion=vim.TaskFilterSpec.RecursionOption.all
        )
    begin = _since(hours)
    if begin is not None:
        spec.time = vim.TaskFilterSpec.ByTime(
            timeType=vim.TaskFilterSpec.TimeOption.startedTime, beginTime=begin
        )
    if states:
        spec.state = [vim.TaskInfo.State(state) for state in states]

    task_manager = require_manager(content.taskManager, "task manager")
    collector = task_manager.CreateCollectorForTasks(filter=spec)
    try:
        collector.SetCollectorPageSize(min(max_count, MAX_PAGE_SIZE))
        tasks = list(collector.latestPage or [])
    finally:
        _destroy(collector)
    ordered = _newest_first(tasks, "startTime", "queueTime", "completeTime")
    return [map_task_info(task) for task in ordered[:max_count]]


def query_triggered_alarms(service_instance: vim.ServiceInstance) -> list[dict[str, Any]]:
    """Every currently triggered alarm across the inventory."""
    records = collect_properties(
        service_instance, vim.ManagedEntity, ["name", "triggeredAlarmState"]
    )
    alarms: list[dict[str, Any]] = []
    alarm_refs: dict[str, Any] = {}
    for record in records:
        for state in record.props.get("triggeredAlarmState") or []:
            mapped = map_triggered_alarm(state, record.get("name"))
            alarms.append(mapped)
            alarm = getattr(state, "alarm", None)
            if alarm is not None and mapped["alarm_moid"]:
                alarm_refs[mapped["alarm_moid"]] = alarm

    if alarm_refs:
        definitions = collect_properties_for(
            service_instance,
            vim.alarm.Alarm,
            list(alarm_refs.values()),
            ["info.name", "info.description"],
        )
        names = {
            record.moid: (record.get("info.name"), record.get("info.description"))
            for record in definitions
        }
        for alarm in alarms:
            name, description = names.get(alarm["alarm_moid"] or "", (None, None))
            alarm["alarm"] = name
            alarm["description"] = description
    return alarms


def entity_reference(service_instance: vim.ServiceInstance, moid: str, vim_type: type) -> Any:
    return vim_type(moid, service_instance._stub)


def recent_task_summary(service_instance: vim.ServiceInstance) -> list[dict[str, Any]]:
    """Tasks vCenter currently considers 'recent', including running ones."""
    content = service_instance.RetrieveContent()
    task_manager = require_manager(content.taskManager, "task manager")
    tasks = []
    for task in task_manager.recentTask or []:
        try:
            tasks.append(map_task_info(task.info))
        except Exception:
            logger.debug("Skipping task %s that disappeared", moid_of(task), exc_info=True)
    return tasks
