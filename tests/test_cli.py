"""Command line behaviour."""

from __future__ import annotations

import json

import pytest

from fake_vsphere import FakeSession, build_inventory
from vmware_mcp import cli
from vmware_mcp.config import PermissionMode
from vmware_mcp.vsphere.client import VSphereClient

ENV = {
    "VMWARE_HOST": "vcenter.lab.local",
    "VMWARE_USERNAME": "svc@vsphere.local",
    "VMWARE_PASSWORD": "pw",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in list(cli.os.environ):
        if key.startswith(("VMWARE_", "VSPHERE_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_defaults_come_from_the_environment():
    settings = cli.settings_from_args(parse([]))
    assert settings.host == "vcenter.lab.local"
    assert settings.permission_mode is PermissionMode.READ_ONLY
    assert settings.verify_ssl is True


def test_flags_override_the_environment():
    settings = cli.settings_from_args(
        parse(
            [
                "--vsphere-host",
                "esxi-01.lab.local",
                "--vsphere-port",
                "8443",
                "--username",
                "root",
                "--permission-mode",
                "write",
                "--insecure",
            ]
        )
    )
    assert settings.host == "esxi-01.lab.local"
    assert settings.port == 8443
    assert settings.username == "root"
    assert settings.permission_mode is PermissionMode.WRITE
    assert settings.verify_ssl is False
    # Unmentioned values still come from the environment.
    assert settings.password == "pw"


def test_the_default_transport_is_stdio():
    assert parse([]).transport == "stdio"
    assert parse(["--transport", "streamable-http", "--port", "9000"]).port == 9000


def test_the_transport_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("VMWARE_TRANSPORT", "streamable-http")
    assert parse([]).transport == "streamable-http"


def test_missing_configuration_exits_with_a_message(monkeypatch, capsys):
    monkeypatch.delenv("VMWARE_HOST")
    assert cli.main([]) == 2
    assert "VMWARE_HOST is required" in capsys.readouterr().err


def test_check_prints_a_summary_and_succeeds(monkeypatch, capsys):
    inventory = build_inventory()
    session = FakeSession(inventory)
    monkeypatch.setattr(
        cli, "VSphereClient", lambda settings: VSphereClient(settings, session=session)
    )

    assert cli.main(["--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["endpoint"] == "vcenter.lab.local:443"
    assert report["authenticated_as"] == "svc-mcp@vsphere.local"
    assert report["server"]["version"] == "8.0.3"
    assert report["inventory_objects_indexed"] > 0
    assert session.closed is True


def test_check_reports_a_connection_failure(monkeypatch, capsys):
    from vmware_mcp.errors import ConnectionFailedError

    class Failing:
        def __init__(self, settings):
            pass

        async def about(self):
            raise ConnectionFailedError("vSphere rejected the credentials")

        async def close(self):
            return None

    monkeypatch.setattr(cli, "VSphereClient", Failing)
    assert cli.main(["--check"]) == 1
    assert "rejected the credentials" in capsys.readouterr().err


def test_logging_goes_to_stderr_so_stdio_transport_stays_clean(monkeypatch):
    captured = {}

    def fake_basic_config(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli.logging, "basicConfig", fake_basic_config)
    cli.configure_logging("DEBUG")
    assert captured["stream"] is cli.sys.stderr
    assert captured["level"] == cli.logging.DEBUG


def test_main_starts_the_requested_transport(monkeypatch):
    started = {}

    class DummyServer:
        def run(self, transport, **kwargs):
            started["transport"] = transport
            started["kwargs"] = kwargs

    monkeypatch.setattr("vmware_mcp.server.create_server", lambda settings: DummyServer())
    assert cli.main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9100"]) == 0
    assert started == {"transport": "streamable-http", "kwargs": {"host": "0.0.0.0", "port": 9100}}


def test_stdio_transport_is_started_without_network_arguments(monkeypatch):
    started = {}

    class DummyServer:
        def run(self, transport, **kwargs):
            started["transport"] = transport
            started["kwargs"] = kwargs

    monkeypatch.setattr("vmware_mcp.server.create_server", lambda settings: DummyServer())
    assert cli.main([]) == 0
    assert started == {"transport": "stdio", "kwargs": {}}
