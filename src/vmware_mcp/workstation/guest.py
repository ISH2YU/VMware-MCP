"""Guest OS automation via ``vmrun``: run commands, copy files, wait for Tools."""

from __future__ import annotations

import logging
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from ..config import Settings
from ..errors import GuestOperationError, InvalidArgumentError
from .vmrun import GuestAuth, VmrunRunner
from .vmx import guest_os_family

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuestCommandResult:
    """Outcome of a command run inside the guest."""

    program: str
    arguments: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "arguments": self.arguments,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
        }


class GuestOps:
    """Run programs and move files through VMware Tools."""

    def __init__(self, runner: VmrunRunner, settings: Settings) -> None:
        self._runner = runner
        self._settings = settings

    def resolve_auth(
        self,
        username: str | None = None,
        password: str | None = None,
    ) -> GuestAuth:
        user = username if username is not None else self._settings.guest_username
        if not user:
            raise InvalidArgumentError(
                "Guest operations need credentials. Set VMWARE_GUEST_USERNAME and "
                "VMWARE_GUEST_PASSWORD, or pass username/password to the tool."
            )
        if password is not None:
            pwd = password
        elif username is not None:
            # Explicit username without password: empty password is intentional
            # (some lab images have blank passwords).
            pwd = ""
        else:
            pwd = self._settings.guest_password or ""
        return GuestAuth(username=user, password=pwd)

    async def tools_state(self, vmx: Path) -> str:
        """``running`` / ``installed`` / ``notInstalled`` / ``unknown``."""
        result = await self._runner.run("checkToolsState", str(vmx), check=False, timeout=30)
        text = (result.stdout or result.stderr).strip().lower()
        if "running" in text:
            return "running"
        if "installed" in text:
            return "installed"
        if "not" in text:
            return "notInstalled"
        return text or "unknown"

    async def wait_for_tools(
        self, vmx: Path, *, timeout: float | None = None, poll_seconds: float = 2.0
    ) -> str:
        limit = _positive_timeout(timeout, self._settings.boot_timeout)
        with anyio.move_on_after(limit) as scope:
            while True:
                state = await self.tools_state(vmx)
                if state == "running":
                    return state
                await anyio.sleep(poll_seconds)
        if scope.cancelled_caught:
            raise GuestOperationError(
                f"VMware Tools did not become ready within {limit:g}s for {vmx.name}. "
                f"Is Tools installed in the guest, and has the VM finished booting?"
            )
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_ip(self, vmx: Path) -> str | None:
        result = await self._runner.run("getGuestIPAddress", str(vmx), check=False, timeout=30)
        if result.exit_code != 0:
            # -wait variant is separate; without it vmrun returns an error when
            # Tools has no address yet.
            return None
        address = result.output.strip()
        return address or None

    async def wait_for_ip(
        self, vmx: Path, *, timeout: float | None = None, poll_seconds: float = 2.0
    ) -> str:
        limit = _positive_timeout(timeout, self._settings.boot_timeout)
        with anyio.move_on_after(limit) as scope:
            # Prefer the blocking form; fall back to polling if the host's vmrun
            # is too old to support -wait.
            result = await self._runner.run(
                "getGuestIPAddress", str(vmx), "-wait", check=False, timeout=limit
            )
            if result.exit_code == 0 and result.output.strip():
                return result.output.strip()
            while True:
                address = await self.get_ip(vmx)
                if address:
                    return address
                await anyio.sleep(poll_seconds)
        if scope.cancelled_caught:
            raise GuestOperationError(
                f"No guest IP address within {limit:g}s for {vmx.name}. "
                f"The guest may still be booting, or it has no network."
            )
        raise AssertionError("unreachable")  # pragma: no cover

    async def wait_for_guest(
        self,
        vmx: Path,
        *,
        wait_for_ip: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for Tools, then optionally an IP, under a single deadline."""
        limit = _positive_timeout(timeout, self._settings.boot_timeout)
        deadline = anyio.current_time() + limit
        tools = await self.wait_for_tools(vmx, timeout=limit)
        ip: str | None = None
        if wait_for_ip:
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                raise GuestOperationError(
                    f"VMware Tools became ready but the {limit:g}s wait expired before "
                    f"an IP was available for {vmx.name}."
                )
            ip = await self.wait_for_ip(vmx, timeout=remaining)
        return {"tools_state": tools, "ip_address": ip}

    async def run_program(
        self,
        vmx: Path,
        program: str,
        arguments: str = "",
        *,
        auth: GuestAuth,
        guest_os: str | None = None,
        interactive: bool = False,
        no_wait: bool = False,
        timeout: float | None = None,
    ) -> GuestCommandResult:
        """Run a program in the guest and capture stdout/stderr when possible.

        ``vmrun runProgramInGuest`` itself does not return output. For capture
        we wrap the command so the guest writes stdout/stderr to temp files,
        then pull those files back. ``no_wait`` skips capture entirely.
        """
        if not program or not program.strip():
            raise InvalidArgumentError("program must not be empty.")
        if no_wait:
            args = [str(vmx), "-noWait"]
            if interactive:
                args.append("-interactive")
            args += [program]
            if arguments:
                args.append(arguments)
            await self._runner.run(
                "runProgramInGuest",
                *args,
                auth=auth,
                timeout=timeout or self._settings.guest_timeout,
            )
            return GuestCommandResult(
                program=program, arguments=arguments, exit_code=None, stdout="", stderr=""
            )

        family = guest_os_family(guest_os) or "windows"
        if family == "windows":
            return await self._run_windows(
                vmx, program, arguments, auth=auth, interactive=interactive, timeout=timeout
            )
        return await self._run_posix(
            vmx, program, arguments, auth=auth, interactive=interactive, timeout=timeout
        )

    async def run_script(
        self,
        vmx: Path,
        interpreter: str,
        script_text: str,
        *,
        auth: GuestAuth,
        guest_os: str | None = None,
        timeout: float | None = None,
    ) -> GuestCommandResult:
        """Write a script into the guest and run it with ``interpreter``."""
        family = guest_os_family(guest_os) or "windows"
        if family == "windows":
            remote = r"C:\Windows\Temp\vmware-mcp-script.cmd"
            await self._write_text(vmx, remote, script_text.replace("\n", "\r\n"), auth=auth)
            return await self.run_program(
                vmx,
                interpreter or "cmd.exe",
                f'/C "{remote}"',
                auth=auth,
                guest_os=guest_os,
                timeout=timeout,
            )
        remote = "/tmp/vmware-mcp-script.sh"
        await self._write_text(vmx, remote, script_text, auth=auth)
        await self.run_program(
            vmx, "/bin/chmod", f"+x {remote}", auth=auth, guest_os=guest_os, timeout=30
        )
        return await self.run_program(
            vmx,
            interpreter or "/bin/sh",
            remote,
            auth=auth,
            guest_os=guest_os,
            timeout=timeout,
        )

    async def copy_host_to_guest(
        self, vmx: Path, host_path: str, guest_path: str, *, auth: GuestAuth
    ) -> dict[str, Any]:
        source = Path(host_path).expanduser()
        if not source.is_file():
            raise InvalidArgumentError(f"Host file does not exist: {source}")
        await self._runner.run(
            "CopyFileFromHostToGuest",
            str(vmx),
            str(source),
            guest_path,
            auth=auth,
            timeout=self._settings.guest_timeout,
        )
        return {
            "direction": "host_to_guest",
            "host_path": str(source),
            "guest_path": guest_path,
            "bytes": source.stat().st_size,
        }

    async def copy_guest_to_host(
        self, vmx: Path, guest_path: str, host_path: str, *, auth: GuestAuth
    ) -> dict[str, Any]:
        destination = Path(host_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self._runner.run(
            "CopyFileFromGuestToHost",
            str(vmx),
            guest_path,
            str(destination),
            auth=auth,
            timeout=self._settings.guest_timeout,
        )
        size = destination.stat().st_size if destination.is_file() else 0
        return {
            "direction": "guest_to_host",
            "host_path": str(destination),
            "guest_path": guest_path,
            "bytes": size,
        }

    async def list_directory(self, vmx: Path, guest_path: str, *, auth: GuestAuth) -> list[str]:
        result = await self._runner.run(
            "listDirectoryInGuest", str(vmx), guest_path, auth=auth, timeout=60
        )
        return result.lines

    async def file_exists(self, vmx: Path, guest_path: str, *, auth: GuestAuth) -> bool:
        result = await self._runner.run(
            "fileExistsInGuest", str(vmx), guest_path, auth=auth, check=False, timeout=30
        )
        return result.exit_code == 0

    # -- internals --------------------------------------------------------- #

    async def _run_windows(
        self,
        vmx: Path,
        program: str,
        arguments: str,
        *,
        auth: GuestAuth,
        interactive: bool,
        timeout: float | None,
    ) -> GuestCommandResult:
        token = uuid.uuid4().hex[:8]
        out_remote = rf"C:\Windows\Temp\vmware-mcp-{token}-out.txt"
        err_remote = rf"C:\Windows\Temp\vmware-mcp-{token}-err.txt"
        code_remote = rf"C:\Windows\Temp\vmware-mcp-{token}-code.txt"
        script_remote = rf"C:\Windows\Temp\vmware-mcp-{token}-run.ps1"
        script = _windows_capture_script(program, arguments, out_remote, err_remote, code_remote)
        await self._write_text(vmx, script_remote, script, auth=auth)
        guest_args = [str(vmx)]
        if interactive:
            guest_args.append("-interactive")
        guest_args += [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_remote,
        ]
        await self._runner.run(
            "runProgramInGuest",
            *guest_args,
            auth=auth,
            timeout=timeout or self._settings.guest_timeout,
        )
        stdout, stdout_trunc = await self._read_guest_text(vmx, out_remote, auth=auth)
        stderr, stderr_trunc = await self._read_guest_text(vmx, err_remote, auth=auth)
        code_text, _ = await self._read_guest_text(vmx, code_remote, auth=auth)
        return GuestCommandResult(
            program=program,
            arguments=arguments,
            exit_code=_parse_exit_code(code_text),
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_trunc or stderr_trunc,
        )

    async def _run_posix(
        self,
        vmx: Path,
        program: str,
        arguments: str,
        *,
        auth: GuestAuth,
        interactive: bool,
        timeout: float | None,
    ) -> GuestCommandResult:
        token = uuid.uuid4().hex[:8]
        out_remote = f"/tmp/vmware-mcp-{token}-out.txt"
        err_remote = f"/tmp/vmware-mcp-{token}-err.txt"
        code_remote = f"/tmp/vmware-mcp-{token}-code.txt"
        script_remote = f"/tmp/vmware-mcp-{token}-run.sh"
        script = _posix_capture_script(program, arguments, out_remote, err_remote, code_remote)
        await self._write_text(vmx, script_remote, script, auth=auth)
        guest_args = [str(vmx)]
        if interactive:
            guest_args.append("-interactive")
        guest_args += ["/bin/sh", script_remote]
        await self._runner.run(
            "runProgramInGuest",
            *guest_args,
            auth=auth,
            timeout=timeout or self._settings.guest_timeout,
        )
        stdout, stdout_trunc = await self._read_guest_text(vmx, out_remote, auth=auth)
        stderr, stderr_trunc = await self._read_guest_text(vmx, err_remote, auth=auth)
        code_text, _ = await self._read_guest_text(vmx, code_remote, auth=auth)
        return GuestCommandResult(
            program=program,
            arguments=arguments,
            exit_code=_parse_exit_code(code_text),
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_trunc or stderr_trunc,
        )

    async def _read_guest_text(
        self, vmx: Path, guest_path: str, *, auth: GuestAuth
    ) -> tuple[str, bool]:
        with tempfile.TemporaryDirectory(prefix="vmware-mcp-") as tmp:
            host_path = Path(tmp) / "capture.txt"
            await self._runner.run(
                "CopyFileFromGuestToHost",
                str(vmx),
                guest_path,
                str(host_path),
                auth=auth,
                check=False,
                timeout=60,
            )
            if not host_path.is_file():
                return "", False
            data = host_path.read_bytes()
            limit = self._settings.max_output_bytes
            truncated = len(data) > limit
            text = data[:limit].decode("utf-8", "replace")
            return text, truncated

    async def _write_text(
        self, vmx: Path, guest_path: str, content: str, *, auth: GuestAuth
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="vmware-mcp-") as tmp:
            host_path = Path(tmp) / "upload.txt"
            host_path.write_text(content, encoding="utf-8")
            await self._runner.run(
                "CopyFileFromHostToGuest",
                str(vmx),
                str(host_path),
                guest_path,
                auth=auth,
                timeout=60,
            )


def _positive_timeout(timeout: float | None, default: float) -> float:
    limit = default if timeout is None else float(timeout)
    if limit <= 0:
        raise InvalidArgumentError("timeout must be greater than 0 seconds.")
    return limit


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell literal; the only escape is doubling ``'``."""
    return "'" + value.replace("'", "''") + "'"


def _windows_capture_script(
    program: str, arguments: str, out_remote: str, err_remote: str, code_remote: str
) -> str:
    """Run ``program`` via ProcessStartInfo so cmd metacharacters are not interpreted."""
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$psi = New-Object System.Diagnostics.ProcessStartInfo\n"
        f"$psi.FileName = {_ps_quote(program)}\n"
        f"$psi.Arguments = {_ps_quote(arguments)}\n"
        "$psi.UseShellExecute = $false\n"
        "$psi.RedirectStandardOutput = $true\n"
        "$psi.RedirectStandardError = $true\n"
        "$psi.CreateNoWindow = $true\n"
        "$proc = New-Object System.Diagnostics.Process\n"
        "$proc.StartInfo = $psi\n"
        "[void]$proc.Start()\n"
        "$stdout = $proc.StandardOutput.ReadToEnd()\n"
        "$stderr = $proc.StandardError.ReadToEnd()\n"
        "$proc.WaitForExit()\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(out_remote)}, $stdout)\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(err_remote)}, $stderr)\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(code_remote)}, [string]$proc.ExitCode)\n"
    )


def _posix_capture_script(
    program: str, arguments: str, out_remote: str, err_remote: str, code_remote: str
) -> str:
    """Quote program and each argument so the wrapper shell cannot be hijacked."""
    try:
        tokens = shlex.split(arguments, posix=True) if arguments.strip() else []
    except ValueError as exc:
        raise InvalidArgumentError(f"Could not parse arguments: {exc}") from exc
    cmd = " ".join([shlex.quote(program), *(shlex.quote(token) for token in tokens)])
    return (
        f"{cmd} > {shlex.quote(out_remote)} 2> {shlex.quote(err_remote)}\n"
        f"echo $? > {shlex.quote(code_remote)}\n"
    )


def _parse_exit_code(text: str) -> int | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    return None
