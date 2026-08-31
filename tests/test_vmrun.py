"""Locating vmrun, redacting secrets, and interpreting its output."""

from __future__ import annotations

from pathlib import Path

import pytest

from vmware_mcp.config import Product, Settings
from vmware_mcp.errors import CommandTimeoutError, VmrunError, VmrunNotFoundError
from vmware_mcp.workstation.vmrun import (
    GuestAuth,
    VmrunResult,
    VmrunRunner,
    describe_failure,
    find_vmrun,
    redact,
)


def _result(stdout: str = "", stderr: str = "", code: int = 0) -> VmrunResult:
    return VmrunResult(args=("vmrun",), exit_code=code, stdout=stdout, stderr=stderr)


def test_explicit_path_is_preferred(tmp_path: Path):
    executable = tmp_path / "vmrun"
    executable.write_text("#!/bin/sh\n")
    assert find_vmrun(str(executable)) == executable


def test_explicit_path_must_exist(tmp_path: Path):
    with pytest.raises(VmrunNotFoundError, match="not a file"):
        find_vmrun(str(tmp_path / "missing"))


def test_missing_vmrun_lists_where_it_looked(monkeypatch):
    monkeypatch.setattr("vmware_mcp.workstation.vmrun.shutil.which", lambda _: None)
    monkeypatch.setattr("vmware_mcp.workstation.vmrun.Path.is_file", lambda _: False)
    with pytest.raises(VmrunNotFoundError) as excinfo:
        find_vmrun(None)
    message = str(excinfo.value)
    assert "VMWARE_VMRUN_PATH" in message
    assert "vmrun" in message


def test_path_lookup_is_used_when_available(tmp_path: Path, monkeypatch):
    executable = tmp_path / "vmrun"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr("vmware_mcp.workstation.vmrun.shutil.which", lambda _: str(executable))
    assert find_vmrun(None) == executable


def test_redaction_hides_guest_and_vmx_passwords():
    args = ["vmrun", "-T", "ws", "-vp", "vmxsecret", "-gu", "admin", "-gp", "hunter2", "list"]
    assert redact(args) == (
        "vmrun",
        "-T",
        "ws",
        "-vp",
        "***",
        "-gu",
        "admin",
        "-gp",
        "***",
        "list",
    )


def test_results_never_carry_the_password(tmp_path: Path):
    settings = Settings(vm_dirs=(tmp_path,), guest_password="hunter2", vmx_password="vmxsecret")
    runner = VmrunRunner(settings)
    runner._executable = tmp_path / "vmrun"
    built = runner.build_args("list", auth=GuestAuth("admin", "hunter2"))
    assert "hunter2" in built, "the real command line still needs the password"
    assert "hunter2" not in redact(built)
    assert "vmxsecret" not in redact(built)


def test_auth_flags_are_ordered_before_the_command(tmp_path: Path):
    settings = Settings(vm_dirs=(tmp_path,))
    runner = VmrunRunner(settings)
    runner._executable = tmp_path / "vmrun"
    args = runner.build_args("runProgramInGuest", "vm.vmx", auth=GuestAuth("admin", "pw"))
    assert args.index("-gu") < args.index("runProgramInGuest")
    assert args.index("runProgramInGuest") < args.index("vm.vmx")
    assert args[1:3] == ["-T", "ws"]


def test_failure_detection_covers_stdout_error_banners():
    assert _result(code=1).failed
    assert _result(stdout="Error: something broke").failed
    assert not _result(stdout="Total running VMs: 0").failed


def test_hints_are_appended_to_known_errors():
    text = describe_failure(_result(stderr="Error: VMware Tools are not running", code=1))
    assert "VMware Tools are not running" in text
    assert "vmware_wait_for_guest" in text


def test_unknown_errors_pass_through_unchanged():
    assert describe_failure(_result(stderr="Error: kaboom", code=1)) == "kaboom"


def test_bare_exit_code_still_produces_a_message():
    assert "status 3" in describe_failure(_result(code=3))


def test_lines_strips_blanks():
    assert _result(stdout="a\n\n  b  \n").lines == ["a", "b"]


async def test_run_reports_the_command_that_timed_out(tmp_path: Path):
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    settings = Settings(vm_dirs=(tmp_path,), command_timeout=1)
    runner = VmrunRunner(settings)
    runner._executable = script
    with pytest.raises(CommandTimeoutError) as excinfo:
        await runner.run("list", timeout=0.2)
    assert "vmrun list" in str(excinfo.value)
    assert "VMWARE_COMMAND_TIMEOUT" in str(excinfo.value)


async def test_run_raises_with_vmrun_own_message(tmp_path: Path):
    script = tmp_path / "failing"
    script.write_text("#!/bin/sh\necho 'Error: The virtual machine cannot be found' >&2\nexit 1\n")
    script.chmod(0o755)
    settings = Settings(vm_dirs=(tmp_path,))
    runner = VmrunRunner(settings)
    runner._executable = script
    with pytest.raises(VmrunError) as excinfo:
        await runner.run("start", "ghost.vmx")
    assert "cannot be found" in str(excinfo.value)
    assert excinfo.value.exit_code == 1
    assert excinfo.value.command == "start"


async def test_check_false_returns_the_failure(tmp_path: Path):
    script = tmp_path / "failing"
    script.write_text("#!/bin/sh\nexit 4\n")
    script.chmod(0o755)
    settings = Settings(vm_dirs=(tmp_path,))
    runner = VmrunRunner(settings)
    runner._executable = script
    result = await runner.run("list", check=False)
    assert result.exit_code == 4
    assert result.failed


async def test_concurrency_is_capped(tmp_path: Path):
    """Each fake vmrun adds a byte to a file while it runs; the peak is the concurrency."""
    marker = tmp_path / "concurrent"
    trimmer = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()[:-1])"
    script = tmp_path / "counting"
    script.write_text(
        f'#!/bin/sh\nprintf x >> "{marker}"\nsleep 0.2\npython3 -c "{trimmer}" "{marker}"\n'
    )
    script.chmod(0o755)
    marker.write_text("")
    settings = Settings(vm_dirs=(tmp_path,), max_concurrency=2)
    runner = VmrunRunner(settings)
    runner._executable = script

    import anyio

    peak = 0

    async def watch() -> None:
        nonlocal peak
        for _ in range(30):
            peak = max(peak, len(marker.read_text()))
            await anyio.sleep(0.02)

    async with anyio.create_task_group() as group:
        group.start_soon(watch)
        for _ in range(6):
            group.start_soon(runner.run, "list")

    assert peak <= 2, f"expected at most 2 concurrent vmrun processes, saw {peak}"


def test_version_banner_is_extracted(tmp_path: Path):
    import anyio

    script = tmp_path / "vmrun"
    script.write_text("#!/bin/sh\necho 'vmrun version 1.17.0 build-21581411'\n")
    script.chmod(0o755)
    settings = Settings(vm_dirs=(tmp_path,))
    runner = VmrunRunner(settings)
    runner._executable = script
    assert "vmrun version 1.17.0" in (anyio.run(runner.version) or "")


def test_product_is_exposed(tmp_path: Path):
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), product=Product.FUSION))
    assert runner.product is Product.FUSION
