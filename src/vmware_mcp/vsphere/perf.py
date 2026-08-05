"""Performance counter queries against the vSphere PerformanceManager."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pyVmomi import vim

from .mappers import as_timestamp, map_performance_series
from .query import require_manager

#: Counters worth pulling by default, in ``group.name.rollup`` form.
VM_COUNTERS: tuple[str, ...] = (
    "cpu.usage.average",
    "cpu.usagemhz.average",
    "cpu.ready.summation",
    "mem.usage.average",
    "mem.consumed.average",
    "mem.swapinRate.average",
    "disk.usage.average",
    "net.usage.average",
)

HOST_COUNTERS: tuple[str, ...] = (
    "cpu.usage.average",
    "cpu.usagemhz.average",
    "mem.usage.average",
    "mem.consumed.average",
    "disk.usage.average",
    "disk.maxTotalLatency.latest",
    "net.usage.average",
)

#: Historical rollup intervals published by vCenter, in seconds.
INTERVALS: dict[str, int | None] = {
    "realtime": None,
    "5min": 300,
    "30min": 1800,
    "2hours": 7200,
    "1day": 86400,
}

#: vSphere reports percentages in hundredths of a percent.
_SCALE = {"percent": 0.01}


def counter_table(perf_manager: Any) -> dict[str, Any]:
    """Map ``group.name.rollup`` to the counter definition."""
    table = {}
    for counter in perf_manager.perfCounter or []:
        key = f"{counter.groupInfo.key}.{counter.nameInfo.key}.{counter.rollupType}"
        table[key] = counter
    return table


def scale_values(values: Sequence[int | float], unit: str) -> list[float]:
    factor = _SCALE.get(unit)
    if factor is None:
        return [float(value) for value in values]
    return [round(float(value) * factor, 2) for value in values]


def query_metrics(
    service_instance: vim.ServiceInstance,
    entity: Any,
    counter_names: Sequence[str],
    *,
    max_samples: int = 15,
    interval: str = "realtime",
) -> dict[str, Any]:
    """Query ``counter_names`` for one entity and summarise each series.

    Realtime statistics are only available while the entity's host is
    connected; when they are not, vCenter's 5-minute rollup is used instead.
    """
    content = service_instance.RetrieveContent()
    perf_manager = require_manager(content.perfManager, "performance manager")
    table = counter_table(perf_manager)

    metric_ids = []
    wanted: dict[int, tuple[str, str]] = {}
    unknown: list[str] = []
    for name in counter_names:
        counter = table.get(name)
        if counter is None:
            unknown.append(name)
            continue
        unit = counter.unitInfo.key
        wanted[counter.key] = (name, unit)
        metric_ids.append(vim.PerformanceManager.MetricId(counterId=counter.key, instance=""))

    if not metric_ids:
        return {"interval": interval, "counters": [], "unknown_counters": unknown, "samples": 0}

    interval_id = INTERVALS.get(interval)
    if interval == "realtime":
        summary = perf_manager.QueryPerfProviderSummary(entity=entity)
        interval_id = summary.refreshRate if summary.currentSupported else INTERVALS["5min"]

    spec = vim.PerformanceManager.QuerySpec(
        entity=entity,
        metricId=metric_ids,
        intervalId=interval_id,
        maxSample=max_samples,
    )
    results = perf_manager.QueryPerf(querySpec=[spec]) or []

    series: list[dict[str, Any]] = []
    sample_times: list[Any] = []
    for result in results:
        sample_times = [sample.timestamp for sample in result.sampleInfo or []]
        for metric in result.value or []:
            name, unit = wanted.get(metric.id.counterId, (str(metric.id.counterId), "number"))
            values = scale_values(metric.value or [], unit)
            series.append(map_performance_series(name, unit, values))

    return {
        "interval": interval,
        "interval_seconds": interval_id,
        "samples": len(sample_times),
        "window_start": as_timestamp(sample_times[0]) if sample_times else None,
        "window_end": as_timestamp(sample_times[-1]) if sample_times else None,
        "counters": sorted(series, key=lambda item: item["counter"]),
        "unknown_counters": unknown,
    }
