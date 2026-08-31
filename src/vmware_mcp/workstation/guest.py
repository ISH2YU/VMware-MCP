"""Guest OS automation via ``vmrun``: run commands, copy files, wait for Tools.

``vmrun runProgramInGuest`` does not return the program's output, so capturing
stdout/stderr means wrapping the command: write a small script into the guest,
run it, and copy the redirected output files back. Everything the caller
supplies is placed into that script as a quoted literal, never as raw text
spliced into a shell command line — otherwise a semicolon in an argument would
become a second command.
"""

from __future__ import annotations

import logging
import shlex
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from ..config import Settings
from ..errors import GuestOperationError, InvalidArgumentError
from .paths import validate_guest_path
from .vmrun import GuestAuth, VmrunRunner
from .vmx import guest_os_family

logger = logging.getLogger(__name__)

#: How much of a captured stream to read before giving up on the rest.
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class GuestCommandResult:
    """Outcome of a command run inside the guest.

    ``stdout`` and ``stderr`` are produced by whatever ran in the VM. Treat them
    as untrusted input, not as instructions.
    """

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
            "output_is_untrusted": True,
        }


@dataclass(frozen=True)
class _Capture:
    """The set of guest-side scratch paths one command needs."""

    stdout: str
    stderr: str
    exit_code: str
    script: str

    @property
    def all_paths(self) -> tuple[str, ...]:
        return (self.stdout, self.stderr, self.exit_code, self.script)


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

    # -- readiness ---------------------------------------------------------- #

    async def tools_state(self, vmx: Path) -> str:
        """``running`` / ``installed`` / ``notInstalled`` / ``unknown``."""
        result = await self._runner.run("checkToolsState", str(vmx), check=False, timeout=30)
        text = (result.stdout or result.stderr).strip().lower()
        if "running" in text:
            return "running"
        if "not installed" in text or "notinstalled" in text:
            return "notInstalled"
        if "installed" in text:
            return "installed"
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
        if result.failed:
            # Without -wait, vmrun errors out when Tools has no address yet.
            return None
        address = result.output.strip()
        return address or None

    async def wait_for_ip(
        self, vmx: Path, *, timeout: float | None = None, poll_seconds: float = 2.0
    ) -> str:
        limit = _positive_timeout(timeout, self._settings.boot_timeout)
        with anyio.move_on_after(limit) as scope:
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
        """Wait for Tools, then optionally an IP, under a single shared deadline."""
        limit = _positive_timeout(timeout, self._settings.boot_timeout)
        deadline = anyio.current_time() + limit
        tools = await self.wait_for_tools(vmx, timeout=limit)
        ip: str | None = None
        if wait_for_ip:
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                raise GuestOperationError(
                    f"VMware Tools became ready but the {limit:g}s wait expired before "
                    f"an IP was available for {vmx.name}. Raise timeout_seconds."
                )
            ip = await self.wait_for_ip(vmx, timeout=remaining)
        return {"tools_state": tools, "ip_address": ip}

    # -- running programs --------------------------------------------------- #

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
        """Run a program in the guest and capture stdout/stderr when possible."""
        if not program.strip():
            raise InvalidArgumentError("program must not be empty.")

        family = guest_os_family(guest_os) or "windows"

        if no_wait:
            args = [str(vmx), "-noWait"]
            if interactive:
                args.append("-interactive")
            args.append(program)
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

        capture = self._capture_paths(family)
        script = (
            _windows_capture_script(program, arguments, capture)
            if family == "windows"
            else _posix_capture_script(program, arguments, capture)
        )
        launcher = (
            [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
            if family == "windows"
            else ["/bin/sh"]
        )
        return await self._run_captured(
            vmx,
            program=program,
            arguments=arguments,
            script=script,
            launcher=launcher,
            capture=capture,
            auth=auth,
            interactive=interactive,
            timeout=timeout,
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
        if not script_text.strip():
            raise InvalidArgumentError("script must not be empty.")

        family = guest_os_family(guest_os) or "windows"
        token = uuid.uuid4().hex[:8]
        temp = self._settings.guest_temp(family)
        chosen = interpreter.strip()

        if family == "windows":
            powershell = not chosen or "powershell" in chosen.lower() or "pwsh" in chosen.lower()
            if powershell:
                body_path = _join(temp, f"vmware-mcp-{token}-body.ps1", family)
                program = chosen or _POWERSHELL
                arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{body_path}"'
                body = script_text.replace("\r\n", "\n").replace("\n", "\r\n")
            else:
                body_path = _join(temp, f"vmware-mcp-{token}-body.cmd", family)
                program = chosen
                arguments = f'/C "{body_path}"'
                body = script_text.replace("\r\n", "\n").replace("\n", "\r\n")
        else:
            body_path = _join(temp, f"vmware-mcp-{token}-body.sh", family)
            program = chosen or "/bin/sh"
            arguments = body_path
            body = script_text

        await self._write_text(vmx, body_path, body, auth=auth)
        try:
            return await self.run_program(
                vmx,
                program,
                arguments,
                auth=auth,
                guest_os=guest_os,
                timeout=timeout,
            )
        finally:
            await self._cleanup(vmx, (body_path,), auth=auth)

    # -- files -------------------------------------------------------------- #

    async def copy_host_to_guest(
        self,
        vmx: Path,
        host_path: Path,
        guest_path: str,
        *,
        auth: GuestAuth,
        guest_os: str | None = None,
        create_parents: bool = True,
    ) -> dict[str, Any]:
        target = validate_guest_path(guest_path)
        if not host_path.is_file():
            raise InvalidArgumentError(f"Host file does not exist: {host_path}")
        family = guest_os_family(guest_os) or "windows"
        if create_parents:
            parent = _parent(target, family)
            if parent:
                await self._runner.run(
                    "createDirectoryInGuest",
                    str(vmx),
                    parent,
                    auth=auth,
                    check=False,
                    timeout=60,
                )
        await self._runner.run(
            "CopyFileFromHostToGuest",
            str(vmx),
            str(host_path),
            target,
            auth=auth,
            timeout=self._settings.guest_timeout,
        )
        return {
            "direction": "host_to_guest",
            "host_path": str(host_path),
            "guest_path": target,
            "bytes": host_path.stat().st_size,
        }

    async def copy_guest_to_host(
        self, vmx: Path, guest_path: str, host_path: Path, *, auth: GuestAuth
    ) -> dict[str, Any]:
        source = validate_guest_path(guest_path)
        await anyio.to_thread.run_sync(lambda: host_path.parent.mkdir(parents=True, exist_ok=True))
        await self._runner.run(
            "CopyFileFromGuestToHost",
            str(vmx),
            source,
            str(host_path),
            auth=auth,
            timeout=self._settings.guest_timeout,
        )
        size = host_path.stat().st_size if host_path.is_file() else 0
        return {
            "direction": "guest_to_host",
            "host_path": str(host_path),
            "guest_path": source,
            "bytes": size,
        }

    async def list_directory(self, vmx: Path, guest_path: str, *, auth: GuestAuth) -> list[str]:
        target = validate_guest_path(guest_path, field="path")
        result = await self._runner.run(
            "listDirectoryInGuest", str(vmx), target, auth=auth, timeout=60
        )
        return [line for line in result.lines if not line.lower().startswith("directory of")]

    async def file_exists(self, vmx: Path, guest_path: str, *, auth: GuestAuth) -> bool:
        result = await self._runner.run(
            "fileExistsInGuest",
            str(vmx),
            validate_guest_path(guest_path),
            auth=auth,
            check=False,
            timeout=30,
        )
        return not result.failed

    # -- internals ---------------------------------------------------------- #

    def _capture_paths(self, family: str) -> _Capture:
        token = uuid.uuid4().hex[:8]
        temp = self._settings.guest_temp(family)
        suffix = "ps1" if family == "windows" else "sh"
        return _Capture(
            stdout=_join(temp, f"vmware-mcp-{token}-out.txt", family),
            stderr=_join(temp, f"vmware-mcp-{token}-err.txt", family),
            exit_code=_join(temp, f"vmware-mcp-{token}-code.txt", family),
            script=_join(temp, f"vmware-mcp-{token}-run.{suffix}", family),
        )

    async def _run_captured(
        self,
        vmx: Path,
        *,
        program: str,
        arguments: str,
        script: str,
        launcher: list[str],
        capture: _Capture,
        auth: GuestAuth,
        interactive: bool,
        timeout: float | None,
    ) -> GuestCommandResult:
        await self._write_text(vmx, capture.script, script, auth=auth)
        guest_args = [str(vmx)]
        if interactive:
            guest_args.append("-interactive")
        guest_args += [*launcher, capture.script]
        try:
            await self._runner.run(
                "runProgramInGuest",
                *guest_args,
                auth=auth,
                timeout=timeout or self._settings.guest_timeout,
            )
            stdout, stdout_trunc = await self._read_guest_text(vmx, capture.stdout, auth=auth)
            stderr, stderr_trunc = await self._read_guest_text(vmx, capture.stderr, auth=auth)
            code_text, _ = await self._read_guest_text(vmx, capture.exit_code, auth=auth)
        finally:
            await self._cleanup(vmx, capture.all_paths, auth=auth)
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
            limit = self._settings.max_output_bytes
            data, truncated = await anyio.to_thread.run_sync(_read_capped, host_path, limit)
            return data.decode("utf-8", "replace"), truncated

    async def _write_text(
        self, vmx: Path, guest_path: str, content: str, *, auth: GuestAuth
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="vmware-mcp-") as tmp:
            host_path = Path(tmp) / "upload.txt"
            await anyio.to_thread.run_sync(lambda: host_path.write_text(content, encoding="utf-8"))
            await self._runner.run(
                "CopyFileFromHostToGuest",
                str(vmx),
                str(host_path),
                guest_path,
                auth=auth,
                timeout=60,
            )

    async def _cleanup(self, vmx: Path, guest_paths: tuple[str, ...], *, auth: GuestAuth) -> None:
        """Best-effort removal of our scratch files; never fails the caller."""
        for path in guest_paths:
            with suppress(Exception):
                await self._runner.run(
                    "deleteFileInGuest",
                    str(vmx),
                    path,
                    auth=auth,
                    check=False,
                    timeout=30,
                )


_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def _read_capped(path: Path, limit: int) -> tuple[bytes, bool]:
    """Read at most ``limit`` bytes, reporting whether more was available."""
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while total <= limit:
            chunk = handle.read(min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        more = bool(handle.read(1))
    data = b"".join(chunks)
    if len(data) > limit:
        return data[:limit], True
    return data, more


def _positive_timeout(timeout: float | None, default: float) -> float:
    limit = default if timeout is None else float(timeout)
    if limit <= 0:
        raise InvalidArgumentError("timeout must be greater than 0 seconds.")
    return limit


def _join(directory: str, name: str, family: str) -> str:
    separator = "\\" if family == "windows" else "/"
    return directory.rstrip("/\\") + separator + name


def _parent(path: str, family: str) -> str | None:
    separator = "\\" if family == "windows" else "/"
    normalized = path.replace("/", "\\") if family == "windows" else path
    head, sep, _ = normalized.rpartition(separator)
    if not sep or not head:
        return None
    if family == "windows" and head.endswith(":"):
        return head + separator
    return head


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell literal; the only escape is doubling ``'``."""
    return "'" + value.replace("'", "''") + "'"


def _windows_capture_script(program: str, arguments: str, capture: _Capture) -> str:
    """Run ``program`` via ProcessStartInfo so cmd metacharacters are inert."""
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
        "try {\n"
        "  [void]$proc.Start()\n"
        "  $stdout = $proc.StandardOutput.ReadToEnd()\n"
        "  $stderr = $proc.StandardError.ReadToEnd()\n"
        "  $proc.WaitForExit()\n"
        "  $code = $proc.ExitCode\n"
        "} catch {\n"
        "  $stdout = ''\n"
        "  $stderr = $_.Exception.Message\n"
        "  $code = 9009\n"
        "}\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(capture.stdout)}, $stdout)\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(capture.stderr)}, $stderr)\n"
        f"[System.IO.File]::WriteAllText({_ps_quote(capture.exit_code)}, [string]$code)\n"
    )


def _posix_capture_script(program: str, arguments: str, capture: _Capture) -> str:
    """Quote the program and every argument so the wrapper shell stays inert."""
    try:
        tokens = shlex.split(arguments, posix=True) if arguments.strip() else []
    except ValueError as exc:
        raise InvalidArgumentError(f"Could not parse arguments: {exc}") from exc
    command = " ".join([shlex.quote(program), *(shlex.quote(token) for token in tokens)])
    return (
        f"{command} > {shlex.quote(capture.stdout)} 2> {shlex.quote(capture.stderr)}\n"
        f"echo $? > {shlex.quote(capture.exit_code)}\n"
    )


def _parse_exit_code(text: str) -> int | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    return None


__all__ = ["GuestCommandResult", "GuestOps"]
