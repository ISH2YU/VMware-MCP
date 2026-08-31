"""Locating vmrun, redacting secrets, and interpreting its output."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
    settings = Settings(vm_dirs=(tmp_path,), product=Product.WORKSTATION)
    runner = VmrunRunner(settings)
    runner._executable = tmp_path / "vmrun"
    args = runner.build_args("runProgramInGuest", "vm.vmx", auth=GuestAuth("admin", "pw"))
    assert args.index("-gu") < args.index("runProgramInGuest")
    assert args.index("runProgramInGuest") < args.index("vm.vmx")
    assert args[1:3] == ["-T", "ws"]


def test_the_host_type_follows_the_configured_product(tmp_path: Path):
    for product in (Product.WORKSTATION, Product.FUSION, Product.PLAYER):
        runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), product=product))
        runner._executable = tmp_path / "vmrun"
        assert runner.build_args("list")[1:3] == ["-T", product.value]


def test_product_detection_matches_the_platform():
    expected = Product.FUSION if sys.platform == "darwin" else Product.WORKSTATION
    assert Product.detect() is expected


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


def _runner(tmp_path: Path, monkeypatch, **outcome) -> VmrunRunner:
    """A runner whose subprocess layer is replaced by a scripted outcome.

    Faking at the ``run_process`` seam keeps these tests about our own logic and
    lets them run identically on Windows, where a ``#!/bin/sh`` stub would not
    execute at all.
    """
    import anyio

    async def fake_run_process(*_args, **_kwargs):
        delay = outcome.get("sleep", 0)
        if delay:
            await anyio.sleep(delay)
        return SimpleNamespace(
            returncode=outcome.get("returncode", 0),
            stdout=outcome.get("stdout", b""),
            stderr=outcome.get("stderr", b""),
        )

    monkeypatch.setattr("vmware_mcp.workstation.vmrun.anyio.run_process", fake_run_process)
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), **outcome.get("settings", {})))
    runner._executable = tmp_path / "vmrun"
    return runner


async def test_run_reports_the_command_that_timed_out(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, sleep=5)
    with pytest.raises(CommandTimeoutError) as excinfo:
        await runner.run("list", timeout=0.05)
    assert "vmrun list" in str(excinfo.value)
    assert "VMWARE_COMMAND_TIMEOUT" in str(excinfo.value)


async def test_run_raises_with_vmrun_own_message(tmp_path: Path, monkeypatch):
    runner = _runner(
        tmp_path,
        monkeypatch,
        returncode=1,
        stderr=b"Error: The virtual machine cannot be found\n",
    )
    with pytest.raises(VmrunError) as excinfo:
        await runner.run("start", "ghost.vmx")
    assert "cannot be found" in str(excinfo.value)
    assert excinfo.value.exit_code == 1
    assert excinfo.value.command == "start"


async def test_check_false_returns_the_failure(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, returncode=4)
    result = await runner.run("list", check=False)
    assert result.exit_code == 4
    assert result.failed


async def test_output_is_decoded_leniently(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, stdout=b"caf\xe9 \xff\xfe")
    result = await runner.run("list")
    assert "caf" in result.stdout


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
async def test_a_real_subprocess_is_actually_spawned(tmp_path: Path):
    """One genuine end-to-end run, so the faked tests above cannot all be wrong together."""
    script = tmp_path / "vmrun"
    script.write_text('#!/bin/sh\necho "Total running VMs: 0"\nexit 0\n')
    script.chmod(0o755)
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,)))
    runner._executable = script
    result = await runner.run("list")
    assert result.lines == ["Total running VMs: 0"]


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
async def test_a_real_slow_subprocess_hits_the_timeout(tmp_path: Path):
    script = tmp_path / "vmrun"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,)))
    runner._executable = script
    with pytest.raises(CommandTimeoutError):
        await runner.run("list", timeout=0.2)


class _CountingProcess:
    """Stands in for a running subprocess so concurrency can be measured exactly."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, *_args, **_kwargs):
        import anyio

        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await anyio.sleep(0.05)
        finally:
            self.in_flight -= 1
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


@pytest.mark.parametrize("cap", [1, 2, 4])
async def test_concurrency_is_capped(tmp_path: Path, monkeypatch, cap: int):
    import anyio

    counter = _CountingProcess()
    monkeypatch.setattr("vmware_mcp.workstation.vmrun.anyio.run_process", counter)
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), max_concurrency=cap))
    runner._executable = tmp_path / "vmrun"

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(runner.run, "list")

    assert counter.peak == cap, f"expected exactly {cap} concurrent vmrun calls"


async def test_every_call_still_completes_under_the_cap(tmp_path: Path, monkeypatch):
    import anyio

    counter = _CountingProcess()
    monkeypatch.setattr("vmware_mcp.workstation.vmrun.anyio.run_process", counter)
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), max_concurrency=2))
    runner._executable = tmp_path / "vmrun"
    results = []

    async def record() -> None:
        results.append(await runner.run("list"))

    async with anyio.create_task_group() as group:
        for _ in range(6):
            group.start_soon(record)

    assert len(results) == 6
    assert counter.in_flight == 0


async def test_version_banner_is_extracted(tmp_path: Path, monkeypatch):
    runner = _runner(
        tmp_path, monkeypatch, stdout=b"vmrun version 1.17.0 build-21581411\nUsage: vmrun\n"
    )
    assert "vmrun version 1.17.0" in (await runner.version() or "")


async def test_a_missing_version_banner_is_none(tmp_path: Path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, stdout=b"Usage: vmrun\n")
    assert await runner.version() is None


def test_product_is_exposed(tmp_path: Path):
    runner = VmrunRunner(Settings(vm_dirs=(tmp_path,), product=Product.FUSION))
    assert runner.product is Product.FUSION
