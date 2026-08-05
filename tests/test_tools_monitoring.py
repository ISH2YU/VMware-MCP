"""Tasks, events, alarms and performance counters."""

from __future__ import annotations

from conftest import call_ok, call_tool, error_text


async def test_list_events_returns_newest_first(server):
    result = await call_ok(server, "vsphere_list_events")
    assert result["count"] == 3
    assert [event["message"] for event in result["events"]] == [
        "web-01 powered on",
        "web-01 reconfigured",
        "web-01 powered off",
    ]
    assert result["events"][0]["type"] == "vim.event.VmPoweredOnEvent"
    assert result["events"][0]["vm"] == "web-01"
    assert result["events"][0]["user"] == "admin"


async def test_events_are_sorted_by_time_not_by_page_order(server, fake_session):
    # vCenter's documented page ordering is not something to depend on.
    fake_session.instance.events.reverse()
    result = await call_ok(server, "vsphere_list_events")
    assert [event["message"] for event in result["events"]] == [
        "web-01 powered on",
        "web-01 reconfigured",
        "web-01 powered off",
    ]


async def test_tasks_are_sorted_by_time_not_by_page_order(server, fake_session):
    fake_session.instance.tasks.reverse()
    result = await call_ok(server, "vsphere_list_tasks")
    assert [task["operation"] for task in result["tasks"]] == [
        "VirtualMachine.reconfigure",
        "VirtualMachine.powerOn",
    ]


async def test_list_events_passes_filters_through_to_vcenter(server, fake_session):
    await call_ok(
        server, "vsphere_list_events", entity="web-01", hours=6, categories=["error", "warning"]
    )
    spec = fake_session.instance.event_filters[-1]
    assert spec.entity.entity._moId == "vm-101"
    assert spec.category == ["error", "warning"]
    assert spec.time.beginTime is not None


async def test_list_events_limit_becomes_the_collector_page_size(server):
    await call_ok(server, "vsphere_list_events", limit=2)
    result = await call_ok(server, "vsphere_list_events", limit=2)
    assert result["count"] == 2


async def test_list_events_rejects_unknown_categories(server):
    result = await call_tool(server, "vsphere_list_events", categories=["catastrophe"])
    assert result.is_error
    assert "Unknown event categories: catastrophe" in error_text(result)


async def test_list_tasks_reports_state_and_errors(server):
    result = await call_ok(server, "vsphere_list_tasks")
    states = {task["operation"]: task["state"] for task in result["tasks"]}
    assert states == {
        "VirtualMachine.powerOn": "error",
        "VirtualMachine.reconfigure": "success",
    }


async def test_list_running_tasks_uses_the_recent_task_list(server):
    result = await call_ok(server, "vsphere_list_running_tasks")
    assert result["count"] == 1
    assert result["tasks"][0]["state"] == "running"
    assert result["tasks"][0]["progress_percent"] == 42


async def test_get_task_polls_a_single_task(server):
    result = await call_ok(server, "vsphere_get_task", task_id="task-recent")
    assert result["task"]["task_id"] == "task-recent"
    assert result["task"]["state"] == "running"


async def test_list_alarms_sorts_red_first_and_names_the_alarm(server):
    result = await call_ok(server, "vsphere_list_alarms")
    assert result["red_count"] == 1
    assert result["yellow_count"] == 1
    first = result["alarms"][0]
    assert first["status"] == "red"
    assert first["entity"] == "esxi-01.lab.local"
    assert first["alarm"] == "Host memory usage"
    assert first["description"] == "Memory usage is high"


async def test_list_alarms_can_hide_acknowledged_ones(server):
    result = await call_ok(server, "vsphere_list_alarms", include_acknowledged=False)
    assert [alarm["entity"] for alarm in result["alarms"]] == ["esxi-01.lab.local"]


async def test_list_alarms_filters_by_status(server):
    result = await call_ok(server, "vsphere_list_alarms", status="yellow")
    assert result["total_matched"] == 1
    assert result["alarms"][0]["entity"] == "ds-nfs"


async def test_performance_counters_are_scaled_into_real_units(server):
    result = await call_ok(server, "vsphere_get_performance", entity="web-01")
    counters = {counter["counter"]: counter for counter in result["counters"]}
    # vSphere reports percentages in hundredths of a percent.
    assert counters["cpu.usage.average"]["latest"] == 15.0
    assert counters["cpu.usage.average"]["maximum"] == 25.0
    assert counters["cpu.usage.average"]["average"] == 16.67
    # Non-percent counters are passed through untouched.
    assert counters["cpu.usagemhz.average"]["latest"] == 600.0
    assert result["samples"] == 3
    assert result["interval_seconds"] == 20


async def test_performance_uses_the_realtime_refresh_rate(server, fake_session):
    await call_ok(server, "vsphere_get_performance", entity="web-01", samples=5)
    spec = fake_session.instance.performance_manager.queries[-1]
    assert spec.intervalId == 20
    assert spec.maxSample == 5
    assert spec.entity._moId == "vm-101"


async def test_performance_falls_back_to_rollups_without_realtime_stats(server, fake_session):
    fake_session.instance.performance_manager.current_supported = False
    await call_ok(server, "vsphere_get_performance", entity="web-01")
    spec = fake_session.instance.performance_manager.queries[-1]
    assert spec.intervalId == 300


async def test_performance_reports_counters_the_endpoint_does_not_publish(server):
    result = await call_ok(
        server, "vsphere_get_performance", entity="web-01", counters=["gpu.usage.average"]
    )
    assert result["unknown_counters"] == ["gpu.usage.average"]
    assert result["counters"] == []


async def test_performance_rejects_a_zero_sample_request(server):
    result = await call_tool(server, "vsphere_get_performance", entity="web-01", samples=0)
    assert result.is_error
    assert "samples must be at least 1" in error_text(result)


async def test_performance_works_for_hosts_too(server):
    result = await call_ok(
        server, "vsphere_get_performance", entity="esxi-01.lab.local", entity_type="host"
    )
    assert result["moid"] == "host-11"
    assert result["type"] == "host"
