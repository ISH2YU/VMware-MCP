"""Locating and running VMware's ``vmrun`` command line tool.

``vmrun`` ships with Workstation, Fusion and Player and is the supported way to
script a local hypervisor. It is a plain executable, so every call here is a
subprocess; they run on the event loop via anyio rather than blocking it.

Two things make this wrapper more than ``subprocess.run``:

* ``vmrun`` is unhappy when many copies fight over the same VM library, so calls
  are funnelled through a semaphore sized by ``VMWARE_MAX_CONCURRENCY``.
* guest credentials appear on the command line, so the arguments kept on results
  and log lines are redacted at the point of construction rather than at the
  point of printing.
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from ..config import Product, Settings
from ..errors import CommandTimeoutError, VmrunError, VmrunNotFoundError

logger = logging.getLogger(__name__)

#: Flags whose following value must never be logged or stored.
SECRET_FLAGS = frozenset({"-gp", "-vp"})

#: Standard install locations, checked when ``vmrun`` is not on ``PATH``.
KNOWN_LOCATIONS: dict[str, tuple[str, ...]] = {
    "win32": (
        r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
        r"C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
        r"C:\Program Files\VMware\VMware Player\vmrun.exe",
        r"C:\Program Files (x86)\VMware\VMware VIX\vmrun.exe",
        r"C:\Program Files\VMware\VMware VIX\vmrun.exe",
    ),
    "darwin": (
        "/Applications/VMware Fusion.app/Contents/Public/vmrun",
        "/Applications/VMware Fusion Tech Preview.app/Contents/Public/vmrun",
        "/Applications/VMware Fusion.app/Contents/Library/vmrun",
        "/Library/Application Support/VMware Fusion/vmrun",
        "/Applications/VMware Fusion.app/Contents/Library/VMware Fusion.app/Contents/Public/vmrun",
    ),
    "linux": (
        "/usr/bin/vmrun",
        "/usr/local/bin/vmrun",
        "/opt/vmware/bin/vmrun",
        "/usr/lib/vmware/bin/vmrun",
    ),
}

#: Substrings of vmrun's error output mapped to something a human can act on.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "the virtual machine is not powered on",
        "The VM is not running. Start it first with vmware_power_vm.",
    ),
    (
        "vmware tools are not running",
        "VMware Tools is not running in the guest. Guest operations need Tools "
        "installed and running; wait for boot with vmware_wait_for_guest.",
    ),
    (
        "invalid user name or password",
        "The guest credentials were rejected. Check VMWARE_GUEST_USERNAME and "
        "VMWARE_GUEST_PASSWORD, or pass username/password to the tool.",
    ),
    (
        "the virtual machine cannot be found",
        "VMware could not find that .vmx file. It may have been moved or deleted.",
    ),
    (
        "the file already exists",
        "A file at the destination already exists; choose a different name or "
        "remove the existing VM first.",
    ),
    (
        "insufficient permissions",
        "VMware refused the operation for permission reasons. On Linux the user "
        "running the server usually needs to be the VM's owner.",
    ),
    (
        "the snapshot already exists",
        "A snapshot with that name already exists on this VM.",
    ),
    (
        "a file was not found",
        "VMware could not find a file it needed. For guest operations, check the "
        "path exists inside the guest.",
    ),
    (
        "the virtual machine is already powered on",
        "That VM is already running.",
    ),
    (
        "cannot connect to the virtual machine",
        "VMware could not attach to the VM. Make sure Workstation is installed "
        "correctly and the VM is not locked by another process.",
    ),
)


def redact(args: Sequence[str]) -> tuple[str, ...]:
    """Copy of ``args`` with the value after every secret flag replaced."""
    rendered: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            rendered.append("***")
            redact_next = False
            continue
        rendered.append(arg)
        redact_next = arg in SECRET_FLAGS
    return tuple(rendered)


@dataclass(frozen=True)
class VmrunResult:
    """The outcome of one ``vmrun`` invocation.

    ``args`` is already redacted, so a result may be logged or serialised
    without leaking the guest password that was on the command line.
    """

    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout.strip() or self.stderr.strip()

    @property
    def lines(self) -> list[str]:
        return [line.strip() for line in self.stdout.splitlines() if line.strip()]

    @property
    def failed(self) -> bool:
        """vmrun sometimes exits 0 while printing an error to stdout."""
        return self.exit_code != 0 or self.output.lower().startswith("error:")


def find_vmrun(explicit: str | None = None) -> Path:
    """Locate ``vmrun``, preferring an explicitly configured path.

    Raises :class:`VmrunNotFoundError` listing where it looked, because "command
    not found" on its own is a miserable thing to debug.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
        raise VmrunNotFoundError(f"VMWARE_VMRUN_PATH is set to {explicit}, which is not a file.")

    on_path = shutil.which("vmrun")
    if on_path:
        return Path(on_path)

    searched = KNOWN_LOCATIONS.get(sys.platform, KNOWN_LOCATIONS["linux"])
    for location in searched:
        candidate = Path(location)
        if candidate.is_file():
            return candidate

    raise VmrunNotFoundError(
        "Could not find 'vmrun'. It ships with VMware Workstation, Fusion and Player. "
        "Looked on PATH and in: " + ", ".join(searched) + ". "
        "Set VMWARE_VMRUN_PATH to its full path if it lives somewhere else."
    )


def describe_failure(result: VmrunResult) -> str:
    """Turn vmrun's terse output into something actionable."""
    raw = result.output or f"vmrun exited with status {result.exit_code}"
    cleaned = raw.removeprefix("Error: ").strip()
    lowered = cleaned.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in lowered:
            return f"{cleaned} — {hint}"
    return cleaned


@dataclass
class GuestAuth:
    """Guest OS credentials for the ``-gu``/``-gp`` flags."""

    username: str
    password: str = field(default="", repr=False)

    def flags(self) -> list[str]:
        return ["-gu", self.username, "-gp", self.password]


class VmrunRunner:
    """Runs ``vmrun`` subprocesses with timeouts and useful error messages."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._executable: Path | None = None
        self._slots = anyio.Semaphore(max(1, settings.max_concurrency))

    @property
    def product(self) -> Product:
        return self._settings.product

    def executable(self) -> Path:
        if self._executable is None:
            self._executable = find_vmrun(self._settings.vmrun_path)
            logger.info("Using vmrun at %s (-T %s)", self._executable, self.product.value)
        return self._executable

    def build_args(self, command: str, *arguments: str, auth: GuestAuth | None = None) -> list[str]:
        args = [str(self.executable()), "-T", self.product.value]
        if self._settings.vmx_password:
            args += ["-vp", self._settings.vmx_password]
        if auth is not None:
            args += auth.flags()
        args.append(command)
        args.extend(arguments)
        return args

    async def run(
        self,
        command: str,
        *arguments: str,
        auth: GuestAuth | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> VmrunResult:
        """Invoke ``vmrun`` and return its output.

        With ``check`` set, a failure raises :class:`VmrunError` carrying vmrun's
        own message. A failure means a non-zero exit *or* an ``Error:`` banner on
        stdout, because vmrun does both depending on the command.
        """
        args = self.build_args(command, *arguments, auth=auth)
        safe_args = redact(args)
        limit = self._settings.command_timeout if timeout is None else timeout
        logger.debug("Running: %s", " ".join(safe_args))

        async with self._slots:
            try:
                with anyio.fail_after(limit):
                    completed = await anyio.run_process(args, check=False)
            except TimeoutError:
                raise CommandTimeoutError(
                    f"'vmrun {command}' did not finish within {limit:g}s and was stopped. "
                    f"Long operations such as cloning a large VM may need a higher "
                    f"VMWARE_COMMAND_TIMEOUT (or VMWARE_CLONE_TIMEOUT for clones)."
                ) from None

        result = VmrunResult(
            args=safe_args,
            exit_code=completed.returncode,
            stdout=completed.stdout.decode("utf-8", "replace"),
            stderr=completed.stderr.decode("utf-8", "replace"),
        )
        if check and result.failed:
            raise VmrunError(describe_failure(result), command=command, exit_code=result.exit_code)
        return result

    async def version(self) -> str | None:
        """The version banner ``vmrun`` prints when called with no arguments."""
        with anyio.move_on_after(30):
            completed = await anyio.run_process([str(self.executable())], check=False)
            text = completed.stdout.decode("utf-8", "replace") or completed.stderr.decode(
                "utf-8", "replace"
            )
            for line in text.splitlines():
                if "vmrun version" in line.lower():
                    return line.strip()
        return None


__all__ = [
    "KNOWN_LOCATIONS",
    "SECRET_FLAGS",
    "GuestAuth",
    "VmrunResult",
    "VmrunRunner",
    "describe_failure",
    "find_vmrun",
    "redact",
]
