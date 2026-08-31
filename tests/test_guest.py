"""Guest automation: quoting, capture, cleanup and boot waits."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fake_vmrun import FakeVmrun
from vmware_mcp.errors import GuestOperationError, InvalidArgumentError
from vmware_mcp.workstation.client import WorkstationClient
from vmware_mcp.workstation.guest import (
    GuestCommandResult,
    _parent,
    _parse_exit_code,
    _posix_capture_script,
    _ps_quote,
    _read_capped,
    _windows_capture_script,
)

# --------------------------------------------------------------------------- #
# Quoting — the part that stops an argument becoming a second command
# --------------------------------------------------------------------------- #


class _Capture:
    stdout = "/tmp/out.txt"
    stderr = "/tmp/err.txt"
    exit_code = "/tmp/code.txt"
    script = "/tmp/run.sh"


def test_posix_arguments_are_split_into_separate_tokens():
    line = _posix_capture_script("/bin/echo", "one two", _Capture()).split("\n")[0]
    assert line.startswith("/bin/echo one two")


@pytest.mark.parametrize(
    "payload",
    [
        "hello; touch {marker}",
        "x && touch {marker}",
        "$(touch {marker})",
        "`touch {marker}`",
        "a | touch {marker}",
        "> {marker}",
        "x\ntouch {marker}",
    ],
)
def test_posix_injection_does_not_execute(tmp_path: Path, payload: str):
    """Generate the real wrapper, run it with a real shell, prove nothing escaped."""
    marker = tmp_path / "pwned"
    capture = _RealCapture(tmp_path)
    arguments = payload.format(marker=marker)
    script = _posix_capture_script("/bin/echo", arguments, capture)
    script_file = tmp_path / "wrapper.sh"
    script_file.write_text(script)

    subprocess.run(["/bin/sh", str(script_file)], check=False, timeout=30)

    assert not marker.exists(), f"injection succeeded for {arguments!r}"
    printed = Path(capture.stdout).read_text()
    assert "touch" in printed or printed.strip() != "", "the argument should reach the program"


class _RealCapture:
    """Capture paths inside a temp dir, for tests that really run the script."""

    def __init__(self, root: Path) -> None:
        self.stdout = str(root / "out.txt")
        self.stderr = str(root / "err.txt")
        self.exit_code = str(root / "code.txt")
        self.script = str(root / "wrapper.sh")


def test_posix_wrapper_records_the_real_exit_code(tmp_path: Path):
    capture = _RealCapture(tmp_path)
    script = tmp_path / "wrapper.sh"
    script.write_text(_posix_capture_script("/bin/sh", "-c 'exit 7'", capture))
    subprocess.run(["/bin/sh", str(script)], check=False, timeout=30)
    assert _parse_exit_code(Path(capture.exit_code).read_text()) == 7


def test_posix_wrapper_separates_stdout_and_stderr(tmp_path: Path):
    capture = _RealCapture(tmp_path)
    script = tmp_path / "wrapper.sh"
    script.write_text(_posix_capture_script("/bin/sh", "-c 'echo out; echo err >&2'", capture))
    subprocess.run(["/bin/sh", str(script)], check=False, timeout=30)
    assert Path(capture.stdout).read_text().strip() == "out"
    assert Path(capture.stderr).read_text().strip() == "err"


def test_posix_rejects_unbalanced_quotes():
    with pytest.raises(InvalidArgumentError, match="Could not parse"):
        _posix_capture_script("/bin/echo", "unterminated 'quote", _Capture())


def test_windows_wrapper_never_uses_cmd():
    script = _windows_capture_script(r"C:\Tools\app.exe", r"/C echo hi & del C:\evil", _Capture())
    assert "ProcessStartInfo" in script
    assert "UseShellExecute = $false" in script
    assert "cmd.exe /C" not in script


def test_windows_arguments_are_a_powershell_literal():
    payload = r"/quiet & shutdown /s"
    script = _windows_capture_script("setup.exe", payload, _Capture())
    assert _ps_quote(payload) in script


def test_ps_quote_doubles_single_quotes():
    assert _ps_quote("it's") == "'it''s'"
    assert _ps_quote("plain") == "'plain'"


def test_windows_wrapper_records_a_failure_to_launch():
    script = _windows_capture_script("missing.exe", "", _Capture())
    assert "catch {" in script
    assert "9009" in script


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "family", "expected"),
    [
        (r"C:\Temp\app.msi", "windows", r"C:\Temp"),
        (r"C:\app.msi", "windows", "C:\\"),
        ("C:/Temp/app.msi", "windows", r"C:\Temp"),
        ("/tmp/nested/file.txt", "linux", "/tmp/nested"),
        ("/file.txt", "linux", None),
        ("relative.txt", "linux", None),
    ],
)
def test_guest_parent_directory(path, family, expected):
    assert _parent(path, family) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0\n", 0), ("  3 ", 3), ("-1", -1), ("", None), ("not a number", None), ("\n\n5\n", 5)],
)
def test_exit_code_parsing(text, expected):
    assert _parse_exit_code(text) == expected


def test_capped_read_reports_truncation(tmp_path: Path):
    target = tmp_path / "big.txt"
    target.write_bytes(b"a" * 5000)
    data, truncated = _read_capped(target, 1000)
    assert len(data) == 1000
    assert truncated is True
    data, truncated = _read_capped(target, 100_000)
    assert len(data) == 5000
    assert truncated is False


def test_command_result_marks_output_untrusted():
    payload = GuestCommandResult("cmd.exe", "/C dir", 0, "out", "err").to_dict()
    assert payload["output_is_untrusted"] is True


# --------------------------------------------------------------------------- #
# End-to-end against the fake vmrun
# --------------------------------------------------------------------------- #


async def test_windows_command_captures_output(client: WorkstationClient, golden: str):
    result = await client.guest.run_program(
        Path(golden), "cmd.exe", "/C echo hi", auth=client.auth(), guest_os="windows11-64"
    )
    assert result.exit_code == 0
    assert "hello from guest" in result.stdout
    assert result.truncated is False


async def test_linux_command_captures_output(client: WorkstationClient, ubuntu: str):
    result = await client.guest.run_program(
        Path(ubuntu), "/bin/echo", "hi", auth=client.auth(), guest_os="ubuntu-64"
    )
    assert result.exit_code == 0
    assert "hello from guest" in result.stdout


async def test_scratch_files_are_removed_from_the_guest(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    await client.guest.run_program(
        Path(golden), "cmd.exe", "/C dir", auth=client.auth(), guest_os="windows11-64"
    )
    leftovers = [name for name in fake.written_guest_paths(golden) if "vmware-mcp-" in name]
    assert leftovers == [], f"left scratch files behind: {leftovers}"
    assert fake.methods("deleteFileInGuest"), "cleanup should have been attempted"


async def test_each_run_uses_a_fresh_scratch_path(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    for _ in range(2):
        await client.guest.run_program(
            Path(golden), "cmd.exe", "/C dir", auth=client.auth(), guest_os="windows11-64"
        )
    scripts = {
        call.args[1]
        for call in fake.methods("CopyFileFromHostToGuest")
        if call.args[2].endswith("-run.ps1")
    }
    written = {
        call.args[2]
        for call in fake.methods("CopyFileFromHostToGuest")
        if call.args[2].endswith("-run.ps1")
    }
    assert len(written) == 2, "two runs must not share one script path"
    assert scripts, "the wrapper script is uploaded from the host"


async def test_cleanup_still_happens_when_the_program_fails(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    from vmware_mcp.errors import VmrunError

    fake.fail_commands.add("runProgramInGuest")
    with pytest.raises(VmrunError):
        await client.guest.run_program(
            Path(golden), "cmd.exe", "/C dir", auth=client.auth(), guest_os="windows11-64"
        )
    leftovers = [name for name in fake.written_guest_paths(golden) if "vmware-mcp-" in name]
    assert leftovers == []


async def test_output_is_truncated_at_the_configured_limit(
    vm_root: Path, fake: FakeVmrun, golden: str
):
    from conftest import make_client

    fake.program_stdout = b"x" * 50_000
    client = make_client(vm_root, fake, max_output_bytes=2048)
    result = await client.guest.run_program(
        Path(golden), "cmd.exe", "/C dir", auth=client.auth(), guest_os="windows11-64"
    )
    assert len(result.stdout) == 2048
    assert result.truncated is True


async def test_no_wait_skips_capture(client: WorkstationClient, fake: FakeVmrun, golden: str):
    result = await client.guest.run_program(
        Path(golden),
        "setup.exe",
        "/S",
        auth=client.auth(),
        guest_os="windows11-64",
        no_wait=True,
    )
    assert result.exit_code is None
    assert result.stdout == ""
    assert "-noWait" in fake.methods("runProgramInGuest")[0].args


async def test_empty_program_is_rejected(client: WorkstationClient, golden: str):
    with pytest.raises(InvalidArgumentError, match="program must not be empty"):
        await client.guest.run_program(Path(golden), "   ", auth=client.auth())


async def test_run_script_uploads_a_body_and_cleans_up(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    result = await client.guest.run_script(
        Path(golden),
        "",
        "Get-Process\nGet-Service\n",
        auth=client.auth(),
        guest_os="windows11-64",
    )
    assert result.exit_code == 0
    uploaded = [
        call.args[2] for call in fake.methods("CopyFileFromHostToGuest") if "-body." in call.args[2]
    ]
    assert uploaded and uploaded[0].endswith(".ps1")
    assert not [name for name in fake.written_guest_paths(golden) if "-body." in name]


async def test_run_script_uses_cmd_when_asked(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    await client.guest.run_script(
        Path(golden), "cmd.exe", "echo hi", auth=client.auth(), guest_os="windows11-64"
    )
    uploaded = [
        call.args[2] for call in fake.methods("CopyFileFromHostToGuest") if "-body." in call.args[2]
    ]
    assert uploaded[0].endswith(".cmd")


async def test_run_script_writes_crlf_for_windows(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    await client.guest.run_script(
        Path(golden), "", "line one\nline two\n", auth=client.auth(), guest_os="windows11-64"
    )
    _, body = fake.uploaded(golden, "-body.")[0]
    assert b"\r\n" in body


async def test_run_script_uses_sh_on_linux(client: WorkstationClient, fake: FakeVmrun, ubuntu: str):
    await client.guest.run_script(
        Path(ubuntu), "", "echo hi", auth=client.auth(), guest_os="ubuntu-64"
    )
    uploaded = [
        call.args[2] for call in fake.methods("CopyFileFromHostToGuest") if "-body." in call.args[2]
    ]
    assert uploaded[0].endswith(".sh")


async def test_empty_script_is_rejected(client: WorkstationClient, golden: str):
    with pytest.raises(InvalidArgumentError, match="script must not be empty"):
        await client.guest.run_script(Path(golden), "", "   ", auth=client.auth())


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_configured_credentials_are_used(client: WorkstationClient):
    auth = client.auth()
    assert auth.username == "Administrator"
    assert auth.password == "Passw0rd!"


def test_an_explicit_username_means_a_blank_password(client: WorkstationClient):
    auth = client.auth("labuser")
    assert auth.username == "labuser"
    assert auth.password == ""


def test_overrides_win(client: WorkstationClient):
    auth = client.auth("labuser", "s3cret")
    assert (auth.username, auth.password) == ("labuser", "s3cret")


def test_missing_credentials_explain_the_fix(vm_root: Path, fake: FakeVmrun):
    from vmware_mcp.config import PermissionMode, Product, Settings

    settings = Settings(
        vm_dirs=(vm_root,), product=Product.WORKSTATION, permission_mode=PermissionMode.WRITE
    )
    client = WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError, match="VMWARE_GUEST_USERNAME"):
        client.auth()


def test_password_is_not_in_the_repr(client: WorkstationClient):
    assert "Passw0rd!" not in repr(client.auth())


# --------------------------------------------------------------------------- #
# Waiting for boot
# --------------------------------------------------------------------------- #


async def test_wait_for_guest_returns_tools_and_ip(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    fake.tools_state[golden] = "running"
    fake.ips[golden] = "10.0.0.5"
    assert await client.guest.wait_for_guest(Path(golden), timeout=5) == {
        "tools_state": "running",
        "ip_address": "10.0.0.5",
    }


async def test_wait_for_guest_can_skip_the_ip(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    fake.tools_state[golden] = "running"
    result = await client.guest.wait_for_guest(Path(golden), wait_for_ip=False, timeout=5)
    assert result == {"tools_state": "running", "ip_address": None}


async def test_one_deadline_covers_both_waits(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    """Tools is ready but no IP ever arrives: the whole call must respect the budget."""
    import anyio

    fake.tools_state[golden] = "running"
    started = anyio.current_time()
    with pytest.raises(GuestOperationError, match="No guest IP"):
        await client.guest.wait_for_guest(Path(golden), timeout=1)
    elapsed = anyio.current_time() - started
    assert elapsed < 1.9, f"took {elapsed:.2f}s, which is more than the single 1s budget"


async def test_tools_timeout_explains_itself(
    client: WorkstationClient, fake: FakeVmrun, golden: str
):
    fake.tools_state[golden] = "notInstalled"
    with pytest.raises(GuestOperationError, match="VMware Tools did not become ready"):
        await client.guest.wait_for_tools(Path(golden), timeout=0.3, poll_seconds=0.05)


async def test_non_positive_timeouts_are_rejected(client: WorkstationClient, golden: str):
    with pytest.raises(InvalidArgumentError, match="greater than 0"):
        await client.guest.wait_for_guest(Path(golden), timeout=0)
    with pytest.raises(InvalidArgumentError, match="greater than 0"):
        await client.guest.wait_for_tools(Path(golden), timeout=-5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("running", "running"),
        ("installed", "installed"),
        ("not installed", "notInstalled"),
        ("notInstalled", "notInstalled"),
    ],
)
async def test_tools_state_parsing(
    client: WorkstationClient, fake: FakeVmrun, golden: str, raw, expected
):
    fake.tools_state[golden] = raw
    assert await client.guest.tools_state(Path(golden)) == expected


async def test_missing_ip_is_none_not_an_error(client: WorkstationClient, golden: str):
    assert await client.guest.get_ip(Path(golden)) is None


async def test_file_exists_probe(client: WorkstationClient, fake: FakeVmrun, golden: str):
    fake.guest_files.setdefault(golden, {})[r"C:\Temp\a.txt"] = b"x"
    assert await client.guest.file_exists(Path(golden), r"C:\Temp\a.txt", auth=client.auth())
    assert not await client.guest.file_exists(Path(golden), r"C:\Temp\b.txt", auth=client.auth())


async def test_list_directory(client: WorkstationClient, fake: FakeVmrun, golden: str):
    fake.guest_files.setdefault(golden, {}).update({r"C:\Temp\a.txt": b"a", r"C:\Temp\b.txt": b"b"})
    entries = await client.guest.list_directory(Path(golden), r"C:\Temp", auth=client.auth())
    assert sorted(entries) == ["a.txt", "b.txt"]
