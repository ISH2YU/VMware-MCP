"""An in-memory stand-in for VMware's ``vmrun``.

Records every invocation and serves scripted responses, so the real
:class:`~vmware_mcp.workstation.client.WorkstationClient` and tools run
unmodified in tests.

The fake deliberately mirrors awkward real-world behaviour: it stores the
guest-side files a capture wrapper writes, honours ``deleteFileInGuest``, and
can be told to report an error on stdout while still exiting zero, which is
something vmrun really does.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from vmware_mcp.workstation.vmrun import VmrunResult, redact


@dataclass
class FakeCall:
    command: str
    args: list[str]
    auth_user: str | None = None
    auth_password: str | None = None


@dataclass
class FakeVmrun:
    """Drop-in replacement for :class:`VmrunRunner`."""

    executable_path: Path
    product_value: str = "ws"
    calls: list[FakeCall] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    snapshots: dict[str, list[str]] = field(default_factory=dict)
    tools_state: dict[str, str] = field(default_factory=dict)
    ips: dict[str, str] = field(default_factory=dict)
    guest_files: dict[str, dict[str, bytes]] = field(default_factory=dict)
    guest_dirs: dict[str, set[str]] = field(default_factory=dict)
    #: Every host-to-guest copy, kept even after the guest file is deleted.
    uploads: list[tuple[str, str, bytes]] = field(default_factory=list)
    #: Commands that should fail with a non-zero exit code.
    fail_commands: set[str] = field(default_factory=set)
    #: Commands that should print "Error: ..." on stdout but exit zero.
    soft_fail_commands: set[str] = field(default_factory=set)
    #: Captured stdout the fake guest "program" produces.
    program_stdout: bytes = b"hello from guest\n"
    program_stderr: bytes = b""
    program_exit_code: bytes = b"0\n"
    clone_writes_vmx: bool = True
    #: Seconds to yield inside each call, so overlapping work is observable.
    dispatch_delay: float = 0.0
    max_in_flight: int = 0
    _in_flight: int = 0

    @property
    def product(self) -> Any:
        from vmware_mcp.config import Product

        return Product.WORKSTATION

    def executable(self) -> Path:
        return self.executable_path

    def build_args(self, command: str, *arguments: str, auth: Any = None) -> list[str]:
        args = [str(self.executable_path), "-T", self.product_value]
        if auth is not None:
            args += ["-gu", auth.username, "-gp", auth.password]
        args.append(command)
        args.extend(arguments)
        return args

    async def version(self) -> str:
        return "vmrun version 1.17.0 test"

    async def run(
        self,
        command: str,
        *arguments: str,
        auth: Any = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> VmrunResult:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            # A real subprocess suspends the task here; yielding lets tests see
            # whether callers actually overlap their work.
            await anyio.sleep(self.dispatch_delay)
            self.calls.append(
                FakeCall(
                    command=command,
                    args=list(arguments),
                    auth_user=auth.username if auth else None,
                    auth_password=auth.password if auth else None,
                )
            )
            stdout, stderr, code = self._dispatch(command, list(arguments), auth)
        finally:
            self._in_flight -= 1

        result = VmrunResult(
            args=redact(self.build_args(command, *arguments, auth=auth)),
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
        )
        if check and result.failed:
            from vmware_mcp.errors import VmrunError
            from vmware_mcp.workstation.vmrun import describe_failure

            raise VmrunError(describe_failure(result), command=command, exit_code=code)
        return result

    def methods(self, command: str) -> list[FakeCall]:
        return [call for call in self.calls if call.command == command]

    def guest_script(self, vmx: str, suffix: str) -> str | None:
        """The most recent script body written into ``vmx`` ending with ``suffix``."""
        for name, data in reversed(list(self.guest_files.get(vmx, {}).items())):
            if name.endswith(suffix):
                return data.decode()
        return None

    def written_guest_paths(self, vmx: str) -> list[str]:
        return list(self.guest_files.get(vmx, {}))

    def uploaded(self, vmx: str, contains: str) -> list[tuple[str, bytes]]:
        """Every upload to ``vmx`` whose guest path contains ``contains``."""
        return [
            (path, data)
            for target, path, data in self.uploads
            if target == vmx and contains in path
        ]

    def _dispatch(self, command: str, arguments: list[str], auth: Any) -> tuple[str, str, int]:
        if command in self.fail_commands:
            return "", f"Error: simulated failure of {command}", 1
        if command in self.soft_fail_commands:
            return f"Error: simulated soft failure of {command}\n", "", 0

        if command == "list":
            lines = [f"Total running VMs: {len(self.running)}", *sorted(self.running)]
            return "\n".join(lines) + "\n", "", 0

        if command == "start":
            self.running.add(arguments[0])
            return "", "", 0
        if command == "stop":
            self.running.discard(arguments[0])
            return "", "", 0
        if command in {"reset", "suspend", "pause", "unpause"}:
            if command == "suspend":
                self.running.discard(arguments[0])
            return "", "", 0

        if command == "listSnapshots":
            snaps = self.snapshots.get(arguments[0], [])
            lines = [f"Total snapshots: {len(snaps)}", *snaps]
            return "\n".join(lines) + "\n", "", 0
        if command == "snapshot":
            self.snapshots.setdefault(arguments[0], []).append(arguments[1])
            return "", "", 0
        if command == "revertToSnapshot":
            if arguments[1] not in self.snapshots.get(arguments[0], []):
                return "", "Error: The snapshot does not exist", 1
            return "", "", 0
        if command == "deleteSnapshot":
            snaps = self.snapshots.get(arguments[0], [])
            self.snapshots[arguments[0]] = [s for s in snaps if s != arguments[1]]
            return "", "", 0

        if command == "clone":
            source, dest = arguments[0], arguments[1]
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if self.clone_writes_vmx:
                shutil.copy(source, dest_path)
            return "", "", 0

        if command == "deleteVM":
            path = Path(arguments[0])
            if path.is_file():
                path.unlink()
            for sibling in path.parent.glob("*"):
                if sibling.is_file():
                    sibling.unlink()
            self.running.discard(arguments[0])
            return "", "", 0

        if command == "captureScreen":
            Path(arguments[1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return "", "", 0

        if command == "checkToolsState":
            state = self.tools_state.get(arguments[0], "running")
            if state == "error":
                # The real error text contains the word "running", which is a trap
                # for anything that substring-matches the output.
                return (
                    "Error: The VMware Tools are not running in the virtual machine\n",
                    "",
                    255,
                )
            return state + "\n", "", 0

        if command == "getGuestIPAddress":
            ip = self.ips.get(arguments[0])
            if ip:
                return ip + "\n", "", 0
            return "", "Error: The virtual machine is not powered on", 1

        if command == "runProgramInGuest":
            return self._run_program(arguments)

        if command == "createDirectoryInGuest":
            self.guest_dirs.setdefault(arguments[0], set()).add(arguments[1])
            return "", "", 0

        if command == "deleteFileInGuest":
            files = self.guest_files.get(arguments[0], {})
            if arguments[1] in files:
                del files[arguments[1]]
                return "", "", 0
            return "", "Error: A file was not found", 1

        if command == "CopyFileFromHostToGuest":
            vmx, host, guest = arguments[0], arguments[1], arguments[2]
            data = Path(host).read_bytes()
            self.guest_files.setdefault(vmx, {})[guest] = data
            self.uploads.append((vmx, guest, data))
            return "", "", 0

        if command == "CopyFileFromGuestToHost":
            vmx, guest, host = arguments[0], arguments[1], arguments[2]
            data = self.guest_files.get(vmx, {}).get(guest)
            if data is None:
                return "", "Error: A file was not found", 1
            Path(host).write_bytes(data)
            return "", "", 0

        if command == "listDirectoryInGuest":
            vmx, path = arguments[0], arguments[1]
            entries = [name for name in self.guest_files.get(vmx, {}) if name.startswith(path)]
            # vmrun prefixes the listing with a count line.
            lines = [f"Directory list: {len(entries)}", *(_basename(n) for n in entries)]
            return "\n".join(lines) + "\n", "", 0

        if command == "fileExistsInGuest":
            vmx, path = arguments[0], arguments[1]
            # vmrun answers in prose on stdout and exits zero either way.
            exists = path in self.guest_files.get(vmx, {})
            return ("The file exists.\n" if exists else "The file does not exist.\n", "", 0)

        return "", f"Error: unknown command {command}", 1

    def _run_program(self, arguments: list[str]) -> tuple[str, str, int]:
        args = list(arguments)
        vmx = args.pop(0) if args else ""
        no_wait = False
        while args and args[0] in {"-noWait", "-interactive", "-activeWindow"}:
            no_wait = no_wait or args[0] == "-noWait"
            args.pop(0)
        if no_wait:
            return "", "", 0

        # The capture wrapper is always the last argument: a script we wrote.
        script_path = args[-1] if args else ""
        files = self.guest_files.setdefault(vmx, {})
        body = files.get(script_path, b"").decode("utf-8", "replace")
        stdout_path, stderr_path, code_path = _capture_targets(body, script_path)
        if stdout_path:
            files[stdout_path] = self.program_stdout
        if stderr_path:
            files[stderr_path] = self.program_stderr
        if code_path:
            files[code_path] = self.program_exit_code
        return "", "", 0


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _capture_targets(script: str, script_path: str) -> tuple[str | None, str | None, str | None]:
    """Recover the out/err/code paths the wrapper script redirects into."""
    token = _basename(script_path)
    for marker in ("-run.ps1", "-run.sh"):
        token = token.replace(marker, "")
    directory = script_path[: len(script_path) - len(_basename(script_path))]
    if not token:
        return None, None, None
    return (
        f"{directory}{token}-out.txt",
        f"{directory}{token}-err.txt",
        f"{directory}{token}-code.txt",
    )


def write_vmx(
    directory: Path,
    name: str,
    *,
    guest_os: str = "windows11-64",
    cpus: int = 2,
    memory_mb: int = 4096,
    annotation: str = "",
    display_name: str | None = None,
) -> Path:
    """Create a minimal but realistic ``.vmx`` for tests."""
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.vmx"
    lines = [
        '.encoding = "UTF-8"',
        f'displayName = "{display_name or name}"',
        f'guestOS = "{guest_os}"',
        f'numvcpus = "{cpus}"',
        f'cpuid.coresPerSocket = "{cpus}"',
        f'memsize = "{memory_mb}"',
        'firmware = "efi"',
        f'uuid.bios = "56 4d {name[:8].ljust(8)} 00 00-00 00 00 00 00 00 00 01"',
        'ethernet0.present = "TRUE"',
        'ethernet0.connectionType = "nat"',
        'ethernet0.addressType = "generated"',
        'ethernet0.generatedAddress = "00:0c:29:aa:bb:cc"',
        'scsi0.present = "TRUE"',
        'scsi0:0.present = "TRUE"',
        f'scsi0:0.fileName = "{name}.vmdk"',
    ]
    if annotation:
        lines.append(f'annotation = "{annotation}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / f"{name}.vmdk").write_text("fake disk\n")
    return path
