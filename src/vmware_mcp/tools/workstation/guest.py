"""Run commands and move files inside a local guest OS via VMware Tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ...config import PermissionMode
from .._common import ToolContext, mcp_tool

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_wait_for_guest(
        vm: str,
        wait_for_ip: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Wait until VMware Tools is running (and optionally until the guest has an IP).

        Call this after powering on a Windows test VM before running commands
        inside it. Without Tools ready, every guest operation fails.

        ``timeout_seconds`` is a single deadline covering both Tools and IP, not
        two waits stacked back to back.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            wait_for_ip: Also wait for a guest IP address.
            timeout_seconds: Override the default boot timeout for the whole wait.
        """
        resolved = client.resolve(vm)
        ready = await client.guest.wait_for_guest(
            resolved.path, wait_for_ip=wait_for_ip, timeout=timeout_seconds
        )
        return {
            "vm": resolved.name,
            "path": str(resolved.path),
            **ready,
        }

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_run_command(
        vm: str,
        program: str,
        arguments: str = "",
        username: str | None = None,
        password: str | None = None,
        interactive: bool = False,
        no_wait: bool = False,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Run a program inside the guest and return its exit code, stdout and stderr.

        Needs VMware Tools running and guest credentials (``VMWARE_GUEST_USERNAME``
        / ``VMWARE_GUEST_PASSWORD``, or pass them here). On Windows, ``program``
        is typically ``cmd.exe`` or ``powershell.exe``.

        Examples:
        - ``program="cmd.exe"``, ``arguments='/C ipconfig /all'``
        - ``program="powershell.exe"``, ``arguments='-Command Get-Process'``
        - ``program="C:\\Windows\\System32\\msiexec.exe"``, ``arguments='/i C:\\Temp\\app.msi /qn'``

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            program: Absolute path of the program inside the guest, or a shell.
            arguments: Arguments passed to the program.
            username: Guest username override.
            password: Guest password override.
            interactive: Run in the console session (needed for some GUI apps).
            no_wait: Start the program and return immediately, without output.
            timeout_seconds: How long to wait for the program to finish.
        """
        settings.require(PermissionMode.WRITE, "vmware_run_command")
        resolved = client.resolve(vm)
        detail = await client.get_vm(str(resolved.path))
        auth = client.auth(username, password)
        result = await client.guest.run_program(
            resolved.path,
            program,
            arguments,
            auth=auth,
            guest_os=detail.get("guest_os"),
            interactive=interactive,
            no_wait=no_wait,
            timeout=timeout_seconds,
        )
        return {"vm": resolved.name, "path": str(resolved.path), **result.to_dict()}

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_run_script(
        vm: str,
        script: str,
        interpreter: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Upload a short script into the guest and run it.

        Handy for multi-line PowerShell or bash without fighting quoting.
        Defaults to ``cmd.exe /C`` on Windows and ``/bin/sh`` on Linux.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            script: Script body.
            interpreter: Program that runs the script. Defaults by guest OS;
                for PowerShell pass ``powershell.exe``.
            username: Guest username override.
            password: Guest password override.
            timeout_seconds: How long to wait for the script to finish.
        """
        settings.require(PermissionMode.WRITE, "vmware_run_script")
        resolved = client.resolve(vm)
        detail = await client.get_vm(str(resolved.path))
        auth = client.auth(username, password)
        result = await client.guest.run_script(
            resolved.path,
            interpreter or "",
            script,
            auth=auth,
            guest_os=detail.get("guest_os"),
            timeout=timeout_seconds,
        )
        return {"vm": resolved.name, "path": str(resolved.path), **result.to_dict()}

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_copy_to_guest(
        vm: str,
        host_path: str,
        guest_path: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Copy a file from the host into the guest.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            host_path: Path on the machine running this server.
            guest_path: Absolute path inside the guest, e.g. ``C:\\Temp\\app.msi``.
            username: Guest username override.
            password: Guest password override.
        """
        settings.require(PermissionMode.WRITE, "vmware_copy_to_guest")
        resolved = client.resolve(vm)
        auth = client.auth(username, password)
        result = await client.guest.copy_host_to_guest(
            resolved.path, host_path, guest_path, auth=auth
        )
        return {"vm": resolved.name, **result}

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_copy_from_guest(
        vm: str,
        guest_path: str,
        host_path: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Copy a file out of the guest onto the host.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            guest_path: Absolute path inside the guest.
            host_path: Destination path on the machine running this server.
            username: Guest username override.
            password: Guest password override.
        """
        settings.require(PermissionMode.WRITE, "vmware_copy_from_guest")
        resolved = client.resolve(vm)
        auth = client.auth(username, password)
        result = await client.guest.copy_guest_to_host(
            resolved.path, guest_path, host_path, auth=auth
        )
        return {"vm": resolved.name, **result}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vmware_list_guest_directory(
        vm: str,
        path: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """List files in a directory inside the guest.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            path: Absolute directory path inside the guest.
            username: Guest username override.
            password: Guest password override.
        """
        resolved = client.resolve(vm)
        auth = client.auth(username, password)
        entries = await client.guest.list_directory(resolved.path, path, auth=auth)
        return {"vm": resolved.name, "path": path, "entries": entries, "count": len(entries)}
