"""Command line behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_vsphere import FakeSession, build_inventory
from vmware_mcp import cli
from vmware_mcp.config import PermissionMode, Product, VSphereSettings, WorkstationSettings
from vmware_mcp.vsphere.client import VSphereClient

VSPHERE_ENV = {
    "VMWARE_HOST": "vcenter.lab.local",
    "VMWARE_USERNAME": "svc@vsphere.local",
    "VMWARE_PASSWORD": "pw",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in list(cli.os.environ):
        if key.startswith(("VMWARE_", "VSPHERE_")):
            monkeypatch.delenv(key, raising=False)


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_defaults_to_the_workstation_backend():
    settings = cli.settings_from_args(parse([]))
    assert isinstance(settings, WorkstationSettings)
    assert settings.permission_mode is PermissionMode.READ_ONLY


def test_a_host_in_the_environment_selects_vsphere(monkeypatch):
    for key, value in VSPHERE_ENV.items():
        monkeypatch.setenv(key, value)
    settings = cli.settings_from_args(parse([]))
    assert isinstance(settings, VSphereSettings)
    assert settings.host == "vcenter.lab.local"
    assert settings.verify_ssl is True


def test_vsphere_flags_override_the_environment(monkeypatch):
    for key, value in VSPHERE_ENV.items():
        monkeypatch.setenv(key, value)
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
    assert isinstance(settings, VSphereSettings)
    assert settings.host == "esxi-01.lab.local"
    assert settings.port == 8443
    assert settings.username == "root"
    assert settings.permission_mode is PermissionMode.WRITE
    assert settings.verify_ssl is False
    assert settings.password == "pw"


def test_workstation_flags(tmp_path):
    settings = cli.settings_from_args(
        parse(
            [
                "--backend",
                "workstation",
                "--product",
                "ws",
                "--vm-dir",
                str(tmp_path),
                "--guest-user",
                "Administrator",
                "--permission-mode",
                "write",
            ]
        )
    )
    assert isinstance(settings, WorkstationSettings)
    assert settings.product is Product.WORKSTATION
    assert settings.vm_dirs == (Path(tmp_path),)
    assert settings.guest_username == "Administrator"
    assert settings.permission_mode is PermissionMode.WRITE


def test_the_default_transport_is_stdio():
    assert parse([]).transport == "stdio"
    assert parse(["--transport", "streamable-http", "--port", "9000"]).port == 9000


def test_the_transport_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("VMWARE_TRANSPORT", "streamable-http")
    assert parse([]).transport == "streamable-http"


def test_missing_vsphere_configuration_exits_with_a_message(monkeypatch, capsys):
    monkeypatch.setenv("VMWARE_BACKEND", "vsphere")
    assert cli.main([]) == 2
    assert "VMWARE_HOST is required" in capsys.readouterr().err


def test_check_prints_a_vsphere_summary(monkeypatch, capsys):
    for key, value in VSPHERE_ENV.items():
        monkeypatch.setenv(key, value)
    inventory = build_inventory()
    session = FakeSession(inventory)

    async def fake_check(settings):
        client = VSphereClient(settings, session=session)
        try:
            info = await client.about()
            from vmware_mcp.vsphere import mappers

            index = await client.path_index()
            report = {
                "backend": "vsphere",
                "endpoint": settings.endpoint,
                "permission_mode": settings.permission_mode.value,
                "verify_ssl": settings.verify_ssl,
                "authenticated_as": info["session_user"],
                "server": mappers.map_about_info(info["about"]),
                "inventory_objects_indexed": index.size,
            }
            print(json.dumps(report, indent=2))
            return 0
        finally:
            await client.close()

    monkeypatch.setattr(cli, "check_connection", fake_check)
    assert cli.main(["--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["endpoint"] == "vcenter.lab.local:443"
    assert report["authenticated_as"] == "svc-mcp@vsphere.local"
    assert report["server"]["version"] == "8.0.3"
    assert session.closed is True


def test_check_reports_a_connection_failure(monkeypatch, capsys):
    for key, value in VSPHERE_ENV.items():
        monkeypatch.setenv(key, value)
    from vmware_mcp.errors import ConnectionFailedError

    async def boom(settings):
        raise ConnectionFailedError("vSphere rejected the credentials")

    monkeypatch.setattr(cli, "check_connection", boom)
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
    assert started == {
        "transport": "streamable-http",
        "kwargs": {"host": "0.0.0.0", "port": 9100},
    }


def test_stdio_transport_is_started_without_network_arguments(monkeypatch):
    started = {}

    class DummyServer:
        def run(self, transport, **kwargs):
            started["transport"] = transport
            started["kwargs"] = kwargs

    monkeypatch.setattr("vmware_mcp.server.create_server", lambda settings: DummyServer())
    assert cli.main([]) == 0
    assert started == {"transport": "stdio", "kwargs": {}}
