"""An in-memory stand-in for VMware's ``vmrun``.

Records every invocation and serves scripted responses, so the real
:class:`~vmware_mcp.workstation.client.WorkstationClient` and tools run
unmodified in tests.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vmware_mcp.workstation.vmrun import VmrunResult


@dataclass
class FakeCall:
    command: str
    args: list[str]
    auth_user: str | None = None


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
    fail_commands: set[str] = field(default_factory=set)
    clone_delay_written: bool = True

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
        self.calls.append(
            FakeCall(
                command=command,
                args=list(arguments),
                auth_user=auth.username if auth else None,
            )
        )
        stdout, stderr, code = self._dispatch(command, list(arguments), auth)
        result = VmrunResult(
            args=tuple(self.build_args(command, *arguments, auth=auth)),
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
        )
        if check and code != 0:
            from vmware_mcp.errors import VmrunError
            from vmware_mcp.workstation.vmrun import describe_failure

            raise VmrunError(describe_failure(result), command=command, exit_code=code)
        return result

    def methods(self, command: str) -> list[FakeCall]:
        return [call for call in self.calls if call.command == command]

    def _dispatch(self, command: str, arguments: list[str], auth: Any) -> tuple[str, str, int]:
        if command in self.fail_commands:
            return "", f"Error: simulated failure of {command}", 1

        if command == "list":
            lines = [f"Total running VMs: {len(self.running)}", *sorted(self.running)]
            return "\n".join(lines) + "\n", "", 0

        if command == "start":
            self.running.add(arguments[0])
            return "", "", 0
        if command == "stop":
            self.running.discard(arguments[0])
            return "", "", 0
        if command == "reset":
            return "", "", 0
        if command in {"suspend", "pause", "unpause"}:
            return "", "", 0

        if command == "listSnapshots":
            snaps = self.snapshots.get(arguments[0], [])
            lines = [f"Total snapshots: {len(snaps)}", *snaps]
            return "\n".join(lines) + "\n", "", 0
        if command == "snapshot":
            self.snapshots.setdefault(arguments[0], []).append(arguments[1])
            return "", "", 0
        if command == "revertToSnapshot":
            return "", "", 0
        if command == "deleteSnapshot":
            snaps = self.snapshots.get(arguments[0], [])
            self.snapshots[arguments[0]] = [s for s in snaps if s != arguments[1]]
            return "", "", 0

        if command == "clone":
            source, dest = arguments[0], arguments[1]
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if self.clone_delay_written:
                # Copy the source .vmx so the client can rename displayName.
                shutil.copy(source, dest_path)
            return "", "", 0

        if command == "deleteVM":
            path = Path(arguments[0])
            if path.is_file():
                path.unlink()
            self.running.discard(arguments[0])
            return "", "", 0

        if command == "captureScreen":
            Path(arguments[1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return "", "", 0

        if command == "checkToolsState":
            state = self.tools_state.get(arguments[0], "running")
            return state + "\n", "", 0

        if command == "getGuestIPAddress":
            ip = self.ips.get(arguments[0])
            if ip:
                return ip + "\n", "", 0
            return "", "Error: The virtual machine is not powered on", 1

        if command == "runProgramInGuest":
            return self._run_program(arguments, auth)

        if command == "CopyFileFromHostToGuest":
            vmx, host, guest = arguments[0], arguments[1], arguments[2]
            self.guest_files.setdefault(vmx, {})[guest] = Path(host).read_bytes()
            return "", "", 0

        if command == "CopyFileFromGuestToHost":
            vmx, guest, host = arguments[0], arguments[1], arguments[2]
            data = self.guest_files.get(vmx, {}).get(guest)
            if data is None:
                return "", "Error: The file was not found", 1
            Path(host).write_bytes(data)
            return "", "", 0

        if command == "listDirectoryInGuest":
            vmx, path = arguments[0], arguments[1]
            entries = [name for name in self.guest_files.get(vmx, {}) if name.startswith(path)]
            return "\n".join(Path(name).name for name in entries) + "\n", "", 0

        if command == "fileExistsInGuest":
            vmx, path = arguments[0], arguments[1]
            exists = path in self.guest_files.get(vmx, {})
            return ("", "", 0) if exists else ("", "Error: The file was not found", 1)

        return "", f"Error: unknown command {command}", 1

    def _run_program(self, arguments: list[str], auth: Any) -> tuple[str, str, int]:
        # Strip optional flags then: vmx, program, [args...]
        args = list(arguments)
        while args and args[0] in {"-noWait", "-interactive", "-activeWindow"}:
            args.pop(0)
        vmx = args[0]
        program = args[1] if len(args) > 1 else ""
        cmdline = args[2] if len(args) > 2 else ""

        # Our guest helpers write output to known temp files in the guest.
        files = self.guest_files.setdefault(vmx, {})
        if program in {"cmd.exe", "/bin/sh"} or "cmd.exe" in program:
            # Simulate a successful command that wrote capture files.
            if "vmware-mcp-out.txt" in cmdline or "vmware-mcp-out.txt" in str(files):
                pass
            # Windows capture paths
            files[r"C:\Windows\Temp\vmware-mcp-out.txt"] = b"hello from guest\n"
            files[r"C:\Windows\Temp\vmware-mcp-err.txt"] = b""
            files[r"C:\Windows\Temp\vmware-mcp-code.txt"] = b"0\n"
            # Posix capture paths
            files["/tmp/vmware-mcp-out.txt"] = b"hello from guest\n"
            files["/tmp/vmware-mcp-err.txt"] = b""
            files["/tmp/vmware-mcp-code.txt"] = b"0\n"
        return "", "", 0


def write_vmx(
    directory: Path,
    name: str,
    *,
    guest_os: str = "windows11-64",
    cpus: int = 2,
    memory_mb: int = 4096,
    annotation: str = "",
) -> Path:
    """Create a minimal but realistic ``.vmx`` for tests."""
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.vmx"
    lines = [
        '.encoding = "UTF-8"',
        f'displayName = "{name}"',
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
