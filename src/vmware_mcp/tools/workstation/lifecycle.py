"""Clone, reconfigure and delete local VMs — the core of the test-lab workflow."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer

from ...config import PermissionMode
from .._common import DESTRUCTIVE, MUTATING, ToolContext, mcp_tool

CloneType = Literal["full", "linked"]


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_clone_vm(
        vm: str,
        name: str,
        destination_dir: str | None = None,
        clone_type: CloneType = "linked",
        snapshot: str | None = None,
    ) -> dict[str, Any]:
        """Clone a local virtual machine.

        Linked clones are fast and cheap on disk — ideal for spinning up many
        disposable Windows test VMs from one golden image. Full clones are
        independent copies. Cloning from a snapshot (``snapshot=...``) is the
        usual pattern: keep a clean ``golden`` snapshot on the template and
        clone from it.

        The destination must sit inside the configured VM directories.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Source VM (display name, ``.vmx`` path, directory name or UUID).
            name: Display name (and default folder name) for the clone.
            destination_dir: Directory for the clone. Defaults to a sibling of
                the source VM's folder.
            clone_type: ``linked`` (default) or ``full``.
            snapshot: Optional snapshot on the source to clone from.
        """
        settings.require(PermissionMode.WRITE, "vmware_clone_vm")
        return await client.clone_vm(
            vm,
            name,
            destination_dir=destination_dir,
            clone_type=clone_type,
            snapshot=snapshot,
        )

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_clone_many(
        vm: str,
        count: int,
        name_prefix: str,
        destination_dir: str | None = None,
        clone_type: CloneType = "linked",
        snapshot: str | None = None,
        start: bool = False,
        concurrency: int = 1,
    ) -> dict[str, Any]:
        """Clone a local VM many times for parallel testing.

        Creates ``{name_prefix}-01``, ``{name_prefix}-02``, … up to ``count``.
        Continues past individual failures and reports them in ``errors``; a
        clone that is created but fails to power on is still reported as created
        with ``powered_on: false`` and a ``power_error``.

        This is the tool for "give me 10 Windows 11 VMs to test on". Start from
        a golden image with a clean snapshot.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Source / template VM.
            count: How many clones to create.
            name_prefix: Prefix for clone names, e.g. ``win11-test``.
            destination_dir: Parent directory; each clone gets its own subfolder.
                Must be inside the configured VM directories.
            clone_type: ``linked`` (default) or ``full``.
            snapshot: Snapshot on the source to clone from.
            start: Power each clone on after creating it (headless).
            concurrency: How many clones to run at once. Default 1, because
                VMware locks the source disks during a clone and parallel clones
                from one template can fail. Raise it only if it measurably helps.
        """
        settings.require(PermissionMode.WRITE, "vmware_clone_many")
        return await client.clone_many(
            vm,
            count,
            name_prefix=name_prefix,
            destination_dir=destination_dir,
            clone_type=clone_type,
            snapshot=snapshot,
            start=start,
            concurrency=concurrency,
        )

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_reconfigure_vm(
        vm: str,
        name: str | None = None,
        cpu_count: int | None = None,
        cores_per_socket: int | None = None,
        memory_mb: int | None = None,
        annotation: str | None = None,
    ) -> dict[str, Any]:
        """Change a local VM's display name, CPU count, memory or notes.

        Edits the ``.vmx`` directly and atomically. The VM must be powered off.
        Only the supplied fields are changed.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            name: New display name.
            cpu_count: New number of virtual CPUs.
            cores_per_socket: Cores per virtual socket. ``cpu_count`` must be a
                multiple of it.
            memory_mb: New memory size in MiB.
            annotation: Replacement notes text.
        """
        settings.require(PermissionMode.WRITE, "vmware_reconfigure_vm")
        return await client.reconfigure_vm(
            vm,
            name=name,
            cpu_count=cpu_count,
            cores_per_socket=cores_per_socket,
            memory_mb=memory_mb,
            annotation=annotation,
        )

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_delete_vm(vm: str, confirm: bool) -> dict[str, Any]:
        """Permanently delete a local virtual machine and its files.

        This cannot be undone. The VM must be powered off. Requires permission
        mode ``destructive`` and an explicit ``confirm=true``.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            confirm: Must be ``true``.
        """
        settings.require(PermissionMode.DESTRUCTIVE, "vmware_delete_vm")
        return await client.delete_vm(vm, confirm=confirm)

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vmware_delete_many(
        pattern: str, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Permanently delete every VM matching ``pattern``. Cannot be undone.

        Running VMs are stopped first. Always call with ``dry_run=true`` first
        and show the user the match list before deleting anything.

        Requires permission mode ``destructive`` and ``confirm=true``.

        Args:
            pattern: Name filter, e.g. ``web-test-*``. Be specific.
            confirm: Must be ``true`` to actually delete.
            dry_run: Only report which VMs match; delete nothing.
        """
        if not dry_run:
            settings.require(PermissionMode.DESTRUCTIVE, "vmware_delete_many")
        return await client.delete_many(pattern, confirm=confirm, dry_run=dry_run)

    @mcp_tool(server, annotations=MUTATING)
    async def vmware_screenshot(vm: str, destination: str | None = None) -> dict[str, Any]:
        """Capture a PNG screenshot of a running local virtual machine.

        Useful when an AI is debugging a GUI installer or a Windows desktop
        that is not responding to guest commands.

        The destination must be inside ``VMWARE_HOST_WRITE_DIRS``.

        Requires permission mode ``write`` or higher.

        Args:
            vm: Display name, ``.vmx`` path, directory name or BIOS UUID.
            destination: Host path for the PNG. Defaults next to the ``.vmx``.
        """
        settings.require(PermissionMode.WRITE, "vmware_screenshot")
        return await client.screenshot(vm, destination)
