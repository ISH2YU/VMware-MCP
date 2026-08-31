"""Command line behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_vmrun import FakeVmrun, write_vmx
from vmware_mcp import __version__, cli
from vmware_mcp.config import PermissionMode, Product, Settings
from vmware_mcp.workstation import WorkstationClient


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in list(cli.os.environ):
        if key.startswith("VMWARE_"):
            monkeypatch.delenv(key, raising=False)


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_defaults_load_read_only_settings():
    settings = cli.settings_from_args(parse([]))
    assert isinstance(settings, Settings)
    assert settings.permission_mode is PermissionMode.READ_ONLY


def test_flags_override_the_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VMWARE_PERMISSION_MODE", "read-only")
    settings = cli.settings_from_args(
        parse(
            [
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
    assert settings.product is Product.WORKSTATION
    assert settings.vm_dirs == (tmp_path,)
    assert settings.guest_username == "Administrator"
    assert settings.permission_mode is PermissionMode.WRITE


def test_vm_dir_is_repeatable(tmp_path: Path):
    first, second = tmp_path / "a", tmp_path / "b"
    settings = cli.settings_from_args(parse(["--vm-dir", str(first), "--vm-dir", str(second)]))
    assert settings.vm_dirs == (first, second)


def test_the_environment_is_used_when_no_flags_are_given(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VMWARE_VM_DIRS", str(tmp_path))
    monkeypatch.setenv("VMWARE_PERMISSION_MODE", "destructive")
    settings = cli.settings_from_args(parse([]))
    assert settings.vm_dirs == (tmp_path,)
    assert settings.permission_mode is PermissionMode.DESTRUCTIVE


def test_the_default_transport_is_stdio():
    assert parse([]).transport == "stdio"
    assert parse(["--transport", "streamable-http", "--port", "9000"]).port == 9000


def test_the_transport_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("VMWARE_TRANSPORT", "streamable-http")
    assert parse([]).transport == "streamable-http"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        parse(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_a_bad_vmrun_path_exits_with_a_message(tmp_path: Path, capsys):
    assert cli.main(["--vmrun", str(tmp_path / "missing")]) == 2
    assert "not a file" in capsys.readouterr().err


def test_a_bad_permission_mode_is_caught_by_argparse():
    with pytest.raises(SystemExit):
        parse(["--permission-mode", "yolo"])


def test_check_prints_a_summary(monkeypatch, capsys, tmp_path: Path):
    library = tmp_path / "library"
    write_vmx(library, "win11-golden")
    fake = FakeVmrun(executable_path=tmp_path / "vmrun")

    async def fake_check(settings: Settings) -> int:
        client = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
        try:
            print(json.dumps(await client.about(), indent=2))
            return 0
        finally:
            await client.close()

    monkeypatch.setattr(cli, "check_connection", fake_check)
    assert cli.main(["--check", "--vm-dir", str(library), "--product", "ws"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["product"] == "ws"
    assert report["vm_count"] == 1


def test_check_reports_a_failure(monkeypatch, capsys):
    from vmware_mcp.errors import VmrunNotFoundError

    async def boom(settings):
        raise VmrunNotFoundError("Could not find 'vmrun'")

    monkeypatch.setattr(cli, "check_connection", boom)
    assert cli.main(["--check"]) == 1
    assert "vmrun" in capsys.readouterr().err


def test_check_closes_the_client(monkeypatch, tmp_path: Path, capsys):
    library = tmp_path / "library"
    write_vmx(library, "one")
    fake = FakeVmrun(executable_path=tmp_path / "vmrun")
    monkeypatch.setattr(
        "vmware_mcp.workstation.WorkstationClient",
        lambda settings: WorkstationClient(settings, runner=fake),  # type: ignore[arg-type]
    )
    assert cli.main(["--check", "--vm-dir", str(library)]) == 0
    assert json.loads(capsys.readouterr().out)["vm_count"] == 1


def test_logging_goes_to_stderr(monkeypatch):
    captured = {}

    def fake_basic_config(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli.logging, "basicConfig", fake_basic_config)
    cli.configure_logging("DEBUG")
    assert captured["stream"] is cli.sys.stderr


def test_an_unknown_log_level_falls_back_to_info(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kw: captured.update(kw))
    cli.configure_logging("not-a-level")
    assert captured["level"] == cli.logging.INFO


def test_main_starts_stdio_by_default(monkeypatch):
    started = {}

    class DummyServer:
        def run(self, transport, **kwargs):
            started["transport"] = transport
            started["kwargs"] = kwargs

    monkeypatch.setattr("vmware_mcp.server.create_server", lambda settings: DummyServer())
    assert cli.main([]) == 0
    assert started == {"transport": "stdio", "kwargs": {}}


def test_main_starts_http_transport(monkeypatch):
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


def test_module_entry_point_exists():
    from vmware_mcp import __main__

    assert __main__.main is cli.main
