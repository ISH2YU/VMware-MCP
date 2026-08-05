"""Connection handling, fault translation, caching and task waiting."""

from __future__ import annotations

import ssl

import pytest
from pyVmomi import vim, vmodl

from fake_vsphere import FakeSession, build_inventory
from vmware_mcp.errors import (
    ConnectionFailedError,
    ObjectNotFoundError,
    TaskFailedError,
    TaskTimeoutError,
    VMwareMCPError,
)
from vmware_mcp.vsphere import session as session_module
from vmware_mcp.vsphere.client import VSphereClient, translate_fault
from vmware_mcp.vsphere.session import VSphereSession, build_ssl_context
from vmware_mcp.vsphere.tasks import run_task, wait_for_task


def test_ssl_context_can_be_relaxed_for_labs(settings):
    from conftest import make_settings

    strict = build_ssl_context(settings)
    assert strict.verify_mode is ssl.CERT_REQUIRED

    relaxed = build_ssl_context(make_settings(verify_ssl=False))
    assert relaxed.verify_mode is ssl.CERT_NONE
    assert relaxed.check_hostname is False


class RecordingConnect:
    """Stands in for ``SmartConnect``."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        result = self.results.pop(0) if self.results else object()
        if isinstance(result, Exception):
            raise result
        return result


def test_the_session_connects_lazily_and_only_once(settings, monkeypatch):
    connect = RecordingConnect("service-instance")
    monkeypatch.setattr(session_module, "SmartConnect", connect)
    session = VSphereSession(settings)

    assert session.connected is False
    assert connect.calls == 0
    assert session.service_instance() == "service-instance"
    assert session.service_instance() == "service-instance"
    assert connect.calls == 1
    assert session.connected is True


def test_an_expired_session_is_replayed_after_reconnecting(settings, monkeypatch):
    connect = RecordingConnect("first", "second")
    monkeypatch.setattr(session_module, "SmartConnect", connect)
    monkeypatch.setattr(session_module, "Disconnect", lambda instance: None)
    session = VSphereSession(settings)

    attempts = []

    def work(service_instance):
        attempts.append(service_instance)
        if len(attempts) == 1:
            raise vim.fault.NotAuthenticated()
        return f"ran on {service_instance}"

    assert session.call(work) == "ran on second"
    assert attempts == ["first", "second"]
    assert connect.calls == 2


def test_bad_credentials_produce_an_actionable_error(settings, monkeypatch):
    monkeypatch.setattr(session_module, "SmartConnect", RecordingConnect(vim.fault.InvalidLogin()))
    with pytest.raises(ConnectionFailedError) as excinfo:
        VSphereSession(settings).service_instance()
    assert "rejected the credentials" in str(excinfo.value)


def test_a_tls_failure_points_at_the_ca_bundle_setting(settings, monkeypatch):
    monkeypatch.setattr(
        session_module,
        "SmartConnect",
        RecordingConnect(ssl.SSLError("certificate verify failed")),
    )
    with pytest.raises(ConnectionFailedError) as excinfo:
        VSphereSession(settings).service_instance()
    message = str(excinfo.value)
    assert "VMWARE_CA_BUNDLE" in message
    assert "VMWARE_VERIFY_SSL=false" in message


def test_closing_an_unconnected_session_is_a_no_op(settings, monkeypatch):
    monkeypatch.setattr(session_module, "SmartConnect", RecordingConnect())
    session = VSphereSession(settings)
    session.close()
    assert session.connected is False


def test_disconnect_errors_do_not_escape(settings, monkeypatch):
    monkeypatch.setattr(session_module, "SmartConnect", RecordingConnect("instance"))

    def explode(instance):
        raise OSError("connection already gone")

    monkeypatch.setattr(session_module, "Disconnect", explode)
    session = VSphereSession(settings)
    session.service_instance()
    session.close()
    assert session.connected is False


def test_permission_faults_name_the_missing_privilege():
    fault = vim.fault.NoPermission(privilegeId="VirtualMachine.Interact.PowerOn")
    translated = translate_fault(fault)
    assert isinstance(translated, VMwareMCPError)
    assert "VirtualMachine.Interact.PowerOn" in str(translated)


def test_a_vanished_object_translates_to_not_found():
    translated = translate_fault(vmodl.fault.ManagedObjectNotFound())
    assert isinstance(translated, ObjectNotFoundError)


def test_generic_faults_keep_their_message():
    translated = translate_fault(vmodl.MethodFault(msg="something specific went wrong"))
    assert str(translated) == "something specific went wrong"


def test_unrelated_exceptions_are_left_alone():
    assert translate_fault(ValueError("not a vSphere problem")) is None


async def test_client_translates_faults_raised_inside_worker_threads(client):
    def work(service_instance):
        raise vim.fault.NoPermission(privilegeId="System.View")

    with pytest.raises(VMwareMCPError, match=r"System\.View"):
        await client.call(work)


async def test_the_path_index_is_cached_for_the_configured_ttl(inventory, fake_session):
    from conftest import make_settings

    settings = make_settings(cache_ttl=60)
    client = VSphereClient(settings, session=fake_session)

    first = await client.path_index()
    second = await client.path_index()
    assert first is second

    third = await client.path_index(refresh=True)
    assert third is not first
    assert third.size == first.size


async def test_a_zero_ttl_disables_index_caching(inventory, fake_session):
    from conftest import make_settings

    client = VSphereClient(make_settings(cache_ttl=0), session=fake_session)
    assert await client.path_index() is not await client.path_index()


async def test_closing_the_client_closes_the_session(client, fake_session):
    await client.close()
    assert fake_session.closed is True


async def test_properties_for_reports_a_missing_object(client):
    with pytest.raises(ObjectNotFoundError, match="vm-404"):
        await client.properties_for(vim.VirtualMachine, "vm-404", ("name",))


class StubTaskClient:
    """Minimal client stand-in that serves a scripted sequence of task states."""

    def __init__(self, states, settings):
        self.states = list(states)
        self.settings = settings
        self.polls = 0

    async def call(self, func, *args, **kwargs):
        self.polls += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return vim.TaskInfo(
            key="task-77",
            task=vim.Task("task-77", None),
            descriptionId="VirtualMachine.clone",
            state=vim.TaskInfo.State(state),
            progress=100 if state == "success" else 50,
            cancelable=False,
            error=vim.fault.FileNotFound(msg="the disk is missing") if state == "error" else None,
        )


class RecordingReporter:
    def __init__(self):
        self.updates = []

    async def report_progress(self, progress, total=None, message=None):
        self.updates.append((progress, total, message))


async def test_waiting_reports_progress_until_the_task_succeeds(settings):
    client = StubTaskClient(["running", "running", "success"], settings)
    reporter = RecordingReporter()
    result = await wait_for_task(
        client, vim.Task("task-77", None), operation="clone web-01", reporter=reporter
    )
    assert result["state"] == "success"
    assert client.polls == 3
    assert reporter.updates[0][0] == 50
    assert reporter.updates[-1] == (100.0, 100.0, "clone web-01: completed")


async def test_a_failed_task_raises_with_the_vsphere_message(settings):
    client = StubTaskClient(["error"], settings)
    with pytest.raises(TaskFailedError) as excinfo:
        await wait_for_task(client, vim.Task("task-77", None), operation="clone web-01")
    assert "the disk is missing" in str(excinfo.value)


async def test_a_timeout_hands_back_the_task_id_to_poll(settings):
    client = StubTaskClient(["running"], settings)
    with pytest.raises(TaskTimeoutError) as excinfo:
        await wait_for_task(client, vim.Task("task-77", None), operation="clone web-01", timeout=1)
    assert excinfo.value.task_id == "task-77"
    assert "vsphere_get_task" in str(excinfo.value)


async def test_run_task_can_return_before_the_task_finishes(settings):
    client = StubTaskClient(["running"], settings)
    inventory = build_inventory()

    async def call(func, *args, **kwargs):
        return vim.Task("task-77", inventory.stub)

    client.call = call  # type: ignore[method-assign]
    result = await run_task(client, lambda si: None, operation="clone", wait=False)
    assert result == {
        "operation": "clone",
        "task_id": "task-77",
        "status": "running",
        "waited": False,
    }


async def test_a_missing_task_reference_is_reported_clearly(settings):
    client = StubTaskClient(["running"], settings)

    async def call(func, *args, **kwargs):
        return None

    client.call = call  # type: ignore[method-assign]
    with pytest.raises(VMwareMCPError, match="no task reference"):
        await run_task(client, lambda si: None, operation="clone", wait=False)


async def test_the_fake_session_is_wired_the_same_way_as_the_real_one(settings):
    """Guards the test double against drift from VSphereSession's interface."""
    fake = FakeSession(build_inventory())
    for method in ("service_instance", "call", "close"):
        assert callable(getattr(fake, method))
        assert callable(getattr(VSphereSession, method))
