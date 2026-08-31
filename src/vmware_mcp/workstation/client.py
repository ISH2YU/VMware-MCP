"""High-level async client for a local VMware Workstation / Fusion / Player."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Literal

import anyio
import anyio.to_thread

from ..config import Settings
from ..errors import InvalidArgumentError, VMwareMCPError
from .discovery import DiscoveredVm, VmInventory, name_matches
from .guest import GuestAuth, GuestOps
from .paths import (
    normalize_path,
    path_is_within_any,
    require_host_read,
    require_host_write,
    validate_snapshot_name,
    validate_vm_name,
)
from .vmrun import VmrunRunner
from .vmx import apply_config_changes, load_vmx

logger = logging.getLogger(__name__)

PowerAction = Literal[
    "start",
    "stop",
    "reset",
    "suspend",
    "pause",
    "unpause",
    "hard_stop",
    "hard_reset",
]

CloneType = Literal["full", "linked"]

_POWER_MODES: dict[str, tuple[str, str]] = {
    "stop": ("stop", "soft"),
    "hard_stop": ("stop", "hard"),
    "reset": ("reset", "soft"),
    "hard_reset": ("reset", "hard"),
}


class WorkstationClient:
    """Drives local VMs through ``vmrun`` plus direct ``.vmx`` edits."""

    def __init__(
        self,
        settings: Settings,
        runner: VmrunRunner | None = None,
        inventory: VmInventory | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or VmrunRunner(settings)
        self.inventory = inventory or VmInventory(settings.vm_dirs, ttl=settings.cache_ttl)
        self.guest = GuestOps(self.runner, settings)

    # -- meta -------------------------------------------------------------- #

    async def about(self) -> dict[str, Any]:
        version = await self.runner.version()
        vms = await self.inventory.list_async()
        running = await self.list_running()
        return {
            "product": self.settings.product.value,
            "vmrun": str(self.runner.executable()),
            "vmrun_version": version,
            "vm_directories": [str(path) for path in self.settings.vm_dirs],
            "vm_count": len(vms),
            "running_count": len(running),
            "guest_credentials_configured": self.settings.has_guest_credentials,
            "permission_mode": self.settings.permission_mode.value,
            "configuration": self.settings.describe(),
        }

    async def close(self) -> None:
        return None

    # -- inventory --------------------------------------------------------- #

    async def list_vms(
        self,
        *,
        name: str | None = None,
        guest_os: str | None = None,
        guest_os_family: str | None = None,
        running_only: bool = False,
        powered_off_only: bool = False,
    ) -> list[dict[str, Any]]:
        if running_only and powered_off_only:
            raise InvalidArgumentError("running_only and powered_off_only cannot both be true.")
        running_paths = {normalize_path(path) for path in await self.list_running()}
        results = []
        for vm in await self.inventory.list_async():
            if not name_matches(vm.name, name):
                continue
            if guest_os and not name_matches(vm.guest_os, guest_os):
                continue
            if guest_os_family and (vm.guest_os_family or "").lower() != guest_os_family.lower():
                continue
            is_running = normalize_path(vm.path) in running_paths
            if running_only and not is_running:
                continue
            if powered_off_only and is_running:
                continue
            entry = vm.to_dict()
            entry["power_state"] = "poweredOn" if is_running else "poweredOff"
            results.append(entry)
        return sorted(results, key=lambda item: item["name"].lower())

    async def get_vm(self, identifier: str) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        vmx = await anyio.to_thread.run_sync(load_vmx, vm.path)
        detail = vmx.summary()
        powered_on = await self._is_running(vm)
        detail["power_state"] = "poweredOn" if powered_on else "poweredOff"
        if powered_on:
            detail["tools_state"] = await self.guest.tools_state(vm.path)
            detail["ip_address"] = await self.guest.get_ip(vm.path)
        else:
            detail["tools_state"] = "unknown"
            detail["ip_address"] = None
        try:
            detail["snapshots"] = await self.list_snapshots(str(vm.path))
        except VMwareMCPError as exc:
            # Report the whole VM rather than failing, but never imply "none".
            detail["snapshots"] = None
            detail["snapshots_error"] = str(exc)
        return detail

    def resolve(self, identifier: str) -> DiscoveredVm:
        return self.inventory.resolve(identifier)

    async def resolve_async(self, identifier: str) -> DiscoveredVm:
        return await self.inventory.resolve_async(identifier)

    async def find_vms(self, pattern: str) -> list[DiscoveredVm]:
        """Every VM whose display name matches ``pattern`` (substring or glob)."""
        matches = [vm for vm in await self.inventory.list_async() if name_matches(vm.name, pattern)]
        return sorted(matches, key=lambda vm: vm.name.lower())

    # -- power ------------------------------------------------------------- #

    async def list_running(self) -> list[str]:
        result = await self.runner.run("list", check=False, timeout=30)
        paths = []
        for line in result.lines:
            if line.lower().startswith("total running vms"):
                continue
            if not line.lower().endswith(".vmx"):
                continue
            # Only advertise VMs the operator exposed through VMWARE_VM_DIRS.
            # With no directories configured this reports nothing, which is the
            # safe direction: the sandbox fails closed, never open.
            if not path_is_within_any(line, self.settings.vm_dirs):
                continue
            paths.append(line)
        return paths

    async def change_power(
        self, identifier: str, action: PowerAction, *, gui: bool = False
    ) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        return await self._change_power_resolved(vm, action, gui=gui)

    async def _change_power_resolved(
        self, vm: DiscoveredVm, action: PowerAction, *, gui: bool = False
    ) -> dict[str, Any]:
        is_running = await self._is_running(vm)

        if action == "start":
            if is_running:
                return _no_change(vm, "poweredOn", action)
            mode = "gui" if gui else "nogui"
            await self.runner.run("start", str(vm.path), mode)
            return _completed(vm, action, mode=mode)

        if action in {"stop", "hard_stop"}:
            command, mode = _POWER_MODES[action]
            if not is_running:
                return _no_change(vm, "poweredOff", "stop")
            await self.runner.run(command, str(vm.path), mode)
            return _completed(vm, "stop", mode=mode)

        if action in {"reset", "hard_reset"}:
            command, mode = _POWER_MODES[action]
            if not is_running:
                raise InvalidArgumentError(
                    f"{vm.name!r} is not running; start it before resetting."
                )
            await self.runner.run(command, str(vm.path), mode)
            return _completed(vm, "reset", mode=mode)

        if action == "suspend":
            if not is_running:
                return _no_change(vm, "poweredOff", action)
            await self.runner.run("suspend", str(vm.path), "soft")
            return _completed(vm, action)

        if action in {"pause", "unpause"}:
            if not is_running:
                raise InvalidArgumentError(f"{vm.name!r} is not running.")
            await self.runner.run(action, str(vm.path))
            return _completed(vm, action)

        raise InvalidArgumentError(f"Unsupported power action {action!r}.")

    # -- snapshots --------------------------------------------------------- #

    async def list_snapshots(self, identifier: str) -> list[dict[str, Any]]:
        """Snapshot names for one VM.

        A failure is raised rather than reported as "no snapshots": those two
        answers mean very different things to whoever is about to revert.
        """
        vm = await self.inventory.resolve_async(identifier)
        result = await self.runner.run("listSnapshots", str(vm.path), timeout=60)
        return [
            {"name": line}
            for line in result.lines
            if not line.lower().startswith("total snapshots")
        ]

    async def create_snapshot(self, identifier: str, name: str) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        snapshot_name = validate_snapshot_name(name, field="name")
        await self.runner.run("snapshot", str(vm.path), snapshot_name)
        return {
            "vm": vm.name,
            "path": str(vm.path),
            "snapshot": snapshot_name,
            "status": "completed",
        }

    async def revert_snapshot(self, identifier: str, snapshot: str) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        return await self._revert_resolved(vm, snapshot)

    async def _revert_resolved(self, vm: DiscoveredVm, snapshot: str) -> dict[str, Any]:
        snap = validate_snapshot_name(snapshot)
        await self.runner.run("revertToSnapshot", str(vm.path), snap)
        return {"vm": vm.name, "path": str(vm.path), "snapshot": snap, "status": "completed"}

    async def delete_snapshot(
        self, identifier: str, snapshot: str, *, delete_children: bool = False
    ) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        snap = validate_snapshot_name(snapshot)
        args = [str(vm.path), snap]
        if delete_children:
            args.append("andDeleteChildren")
        await self.runner.run("deleteSnapshot", *args)
        return {
            "vm": vm.name,
            "path": str(vm.path),
            "snapshot": snap,
            "deleted_children": delete_children,
            "status": "completed",
        }

    # -- lifecycle --------------------------------------------------------- #

    async def clone_vm(
        self,
        identifier: str,
        name: str,
        *,
        destination_dir: str | None = None,
        clone_type: CloneType = "linked",
        snapshot: str | None = None,
    ) -> dict[str, Any]:
        source = await self.inventory.resolve_async(identifier)
        return await self._clone_resolved(
            source,
            name,
            destination_dir=destination_dir,
            clone_type=clone_type,
            snapshot=snapshot,
        )

    async def _clone_resolved(
        self,
        source: DiscoveredVm,
        name: str,
        *,
        destination_dir: str | None,
        clone_type: CloneType,
        snapshot: str | None,
    ) -> dict[str, Any]:
        clone_name = validate_vm_name(name)
        if clone_type not in ("full", "linked"):
            raise InvalidArgumentError("clone_type must be 'linked' or 'full'.")
        snap = validate_snapshot_name(snapshot) if snapshot else None

        if not self.settings.vm_dirs:
            raise InvalidArgumentError(
                "No VM directories are configured, so there is nowhere safe to put a "
                "clone. Set VMWARE_VM_DIRS."
            )
        target_dir = (
            Path(destination_dir).expanduser().resolve()
            if destination_dir
            else self._default_clone_dir(source, clone_name)
        )
        if not path_is_within_any(target_dir, self.settings.vm_dirs):
            listed = ", ".join(str(path) for path in self.settings.vm_dirs)
            raise InvalidArgumentError(
                f"Clone destination {target_dir} is outside the configured VM directories "
                f"({listed}). Put clones under VMWARE_VM_DIRS."
            )
        target_vmx = target_dir / f"{clone_name}.vmx"
        if target_vmx.exists():
            raise InvalidArgumentError(f"A VM already exists at {target_vmx}.")

        created_dir = not target_dir.exists()
        await anyio.to_thread.run_sync(lambda: target_dir.mkdir(parents=True, exist_ok=True))

        args = [str(source.path), str(target_vmx), clone_type]
        if snap:
            args.append(snap)
        try:
            await self.runner.run("clone", *args, timeout=self.settings.clone_timeout)
        except Exception:
            # Do not leave an empty folder behind for a clone that never existed.
            if created_dir:
                await anyio.to_thread.run_sync(_remove_if_empty, target_dir)
            raise

        # Linked/full clones keep the source display name; rename to what was asked for.
        if target_vmx.is_file():
            await anyio.to_thread.run_sync(_rename_display_name, target_vmx, clone_name)

        self.inventory.invalidate()
        return {
            "source": source.name,
            "source_path": str(source.path),
            "name": clone_name,
            "path": str(target_vmx),
            "clone_type": clone_type,
            "snapshot": snap,
            "status": "completed",
        }

    async def clone_many(
        self,
        identifier: str,
        count: int,
        *,
        name_prefix: str,
        destination_dir: str | None = None,
        clone_type: CloneType = "linked",
        snapshot: str | None = None,
        start: bool = False,
        concurrency: int = 1,
    ) -> dict[str, Any]:
        """Clone one source repeatedly.

        Cloning defaults to one at a time: VMware locks the source VM's disks
        while a clone runs, so parallel clones from the same template can fail
        with lock errors. Raise ``concurrency`` when the template is on fast
        storage and you have measured that it helps.
        """
        if count < 1:
            raise InvalidArgumentError("count must be at least 1.")
        if count > self.settings.max_clone_batch:
            raise InvalidArgumentError(
                f"Refusing to clone more than {self.settings.max_clone_batch} VMs in one "
                f"call. Raise VMWARE_MAX_CLONE_BATCH if you really need more."
            )
        if concurrency < 1:
            raise InvalidArgumentError("concurrency must be at least 1.")

        source = await self.inventory.resolve_async(identifier)
        prefix = validate_vm_name(name_prefix, field="name_prefix")
        parent_dir = Path(destination_dir).expanduser() if destination_dir else None
        width = max(2, len(str(count)))

        created: dict[int, dict[str, Any]] = {}
        errors: dict[int, dict[str, Any]] = {}
        limiter = anyio.CapacityLimiter(min(concurrency, self.settings.max_concurrency))

        async def make_clone(index: int) -> None:
            clone_name = f"{prefix}-{index:0{width}d}"
            async with limiter:
                try:
                    result = await self._clone_resolved(
                        source,
                        clone_name,
                        destination_dir=str(parent_dir / clone_name) if parent_dir else None,
                        clone_type=clone_type,
                        snapshot=snapshot,
                    )
                except Exception as exc:
                    if not isinstance(exc, VMwareMCPError):
                        logger.exception("Clone %s failed", clone_name)
                    errors[index] = {"name": clone_name, "error": str(exc)}
                    return
                created[index] = result
                if not start:
                    return
                # The clone exists either way; a power-on failure is reported
                # against it rather than throwing the whole clone away.
                try:
                    clone_vm = await self.inventory.resolve_async(result["path"])
                    await self._change_power_resolved(clone_vm, "start")
                    result["powered_on"] = True
                except Exception as exc:  # noqa: BLE001 - reported on the clone, not fatal
                    result["powered_on"] = False
                    result["power_error"] = str(exc)

        async with anyio.create_task_group() as group:
            for index in range(1, count + 1):
                group.start_soon(make_clone, index)

        ordered = [created[key] for key in sorted(created)]
        failures = [errors[key] for key in sorted(errors)]
        return {
            "requested": count,
            "created": len(ordered),
            "failed": len(failures),
            "vms": ordered,
            "errors": failures,
        }

    async def reconfigure_vm(
        self,
        identifier: str,
        *,
        name: str | None = None,
        cpu_count: int | None = None,
        cores_per_socket: int | None = None,
        memory_mb: int | None = None,
        annotation: str | None = None,
    ) -> dict[str, Any]:
        if all(
            value is None for value in (name, cpu_count, cores_per_socket, memory_mb, annotation)
        ):
            raise InvalidArgumentError(
                "Nothing to change: supply at least one of name, cpu_count, "
                "cores_per_socket, memory_mb or annotation."
            )
        if name is not None:
            name = validate_vm_name(name)
        vm = await self.inventory.resolve_async(identifier)
        if await self._is_running(vm):
            raise InvalidArgumentError(
                f"{vm.name!r} is powered on. Power it off before changing CPU, memory or name."
            )

        def edit() -> dict[str, Any]:
            vmx = load_vmx(vm.path)
            changes = apply_config_changes(
                vmx,
                name=name,
                cpu_count=cpu_count,
                cores_per_socket=cores_per_socket,
                memory_mb=memory_mb,
                annotation=annotation,
            )
            vmx.write()
            return changes

        changes = await anyio.to_thread.run_sync(edit)
        self.inventory.invalidate()
        return {"vm": vm.name, "path": str(vm.path), "status": "completed", **changes}

    async def delete_vm(self, identifier: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise InvalidArgumentError(
                "Refusing to delete a VM without confirm=true. This removes the .vmx and its disks."
            )
        vm = await self.inventory.resolve_async(identifier)
        return await self._delete_resolved(vm)

    async def _delete_resolved(self, vm: DiscoveredVm) -> dict[str, Any]:
        if await self._is_running(vm):
            raise InvalidArgumentError(f"{vm.name!r} is powered on. Stop it before deleting.")
        await self.runner.run("deleteVM", str(vm.path))
        # deleteVM removes registered files; clean the empty directory if left behind.
        await anyio.to_thread.run_sync(_remove_if_empty, vm.path.parent)
        self.inventory.invalidate()
        return {"vm": vm.name, "path": str(vm.path), "status": "completed"}

    async def screenshot(self, identifier: str, destination: str | None = None) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        raw_target = (
            Path(destination).expanduser()
            if destination
            else vm.path.parent / f"{vm.path.stem}-screenshot.png"
        )
        target = require_host_write(raw_target, self.settings.effective_host_write_dirs())
        await anyio.to_thread.run_sync(lambda: target.parent.mkdir(parents=True, exist_ok=True))
        await self.runner.run("captureScreen", str(vm.path), str(target))
        return {
            "vm": vm.name,
            "path": str(vm.path),
            "screenshot": str(target),
            "bytes": target.stat().st_size if target.is_file() else 0,
        }

    # -- guest file transfer ------------------------------------------------ #

    async def copy_to_guest(
        self,
        identifier: str,
        host_path: str,
        guest_path: str,
        *,
        auth: GuestAuth,
        create_parents: bool = True,
    ) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        source = require_host_read(host_path, self.settings.host_read_dirs)
        result = await self.guest.copy_host_to_guest(
            vm.path,
            source,
            guest_path,
            auth=auth,
            guest_os=vm.guest_os,
            create_parents=create_parents,
        )
        return {"vm": vm.name, **result}

    async def copy_from_guest(
        self, identifier: str, guest_path: str, host_path: str, *, auth: GuestAuth
    ) -> dict[str, Any]:
        vm = await self.inventory.resolve_async(identifier)
        destination = require_host_write(host_path, self.settings.effective_host_write_dirs())
        result = await self.guest.copy_guest_to_host(vm.path, guest_path, destination, auth=auth)
        return {"vm": vm.name, **result}

    # -- batch operations --------------------------------------------------- #

    async def power_many(
        self,
        pattern: str,
        action: PowerAction,
        *,
        gui: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply one power action to every VM matching ``pattern``."""
        return await self._fan_out(
            pattern,
            dry_run=dry_run,
            operation=f"power:{action}",
            work=lambda vm: self._change_power_resolved(vm, action, gui=gui),
        )

    async def revert_many(
        self, pattern: str, snapshot: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Revert every VM matching ``pattern`` to ``snapshot``, stopping it first."""
        snap = validate_snapshot_name(snapshot)

        async def work(vm: DiscoveredVm) -> dict[str, Any]:
            if await self._is_running(vm):
                await self._change_power_resolved(vm, "stop")
            return await self._revert_resolved(vm, snap)

        return await self._fan_out(pattern, dry_run=dry_run, operation=f"revert:{snap}", work=work)

    async def delete_many(
        self, pattern: str, *, confirm: bool, dry_run: bool = False
    ) -> dict[str, Any]:
        """Delete every VM matching ``pattern``. Stops running VMs first."""
        if not confirm and not dry_run:
            raise InvalidArgumentError(
                "Refusing to delete VMs without confirm=true. Use dry_run=true to see "
                "which VMs would be deleted."
            )

        async def work(vm: DiscoveredVm) -> dict[str, Any]:
            if await self._is_running(vm):
                await self._change_power_resolved(vm, "stop")
            return await self._delete_resolved(vm)

        return await self._fan_out(pattern, dry_run=dry_run, operation="delete", work=work)

    async def _fan_out(
        self,
        pattern: str,
        *,
        dry_run: bool,
        operation: str,
        work: Any,
    ) -> dict[str, Any]:
        if not pattern.strip():
            raise InvalidArgumentError("pattern must not be empty.")
        matches = await self.find_vms(pattern)
        summary: dict[str, Any] = {
            "pattern": pattern,
            "operation": operation,
            "matched": len(matches),
            "vms": [{"name": vm.name, "path": str(vm.path)} for vm in matches],
        }
        if dry_run or not matches:
            summary["dry_run"] = True
            summary["succeeded"] = 0
            summary["failed"] = 0
            summary["results"] = []
            summary["errors"] = []
            return summary

        results: dict[int, dict[str, Any]] = {}
        errors: dict[int, dict[str, Any]] = {}
        limiter = anyio.CapacityLimiter(self.settings.max_concurrency)

        async def run_one(index: int, vm: DiscoveredVm) -> None:
            async with limiter:
                try:
                    results[index] = await work(vm)
                except Exception as exc:
                    if not isinstance(exc, VMwareMCPError):
                        logger.exception("%s failed for %s", operation, vm.name)
                    errors[index] = {"vm": vm.name, "path": str(vm.path), "error": str(exc)}

        async with anyio.create_task_group() as group:
            for index, vm in enumerate(matches):
                group.start_soon(run_one, index, vm)

        summary["dry_run"] = False
        summary["succeeded"] = len(results)
        summary["failed"] = len(errors)
        summary["results"] = [results[key] for key in sorted(results)]
        summary["errors"] = [errors[key] for key in sorted(errors)]
        return summary

    # -- helpers ------------------------------------------------------------ #

    def auth(self, username: str | None = None, password: str | None = None) -> GuestAuth:
        return self.guest.resolve_auth(username, password)

    async def _is_running(self, vm: DiscoveredVm) -> bool:
        running = {normalize_path(path) for path in await self.list_running()}
        return normalize_path(vm.path) in running

    def _default_clone_dir(self, source: DiscoveredVm, clone_name: str) -> Path:
        """Sibling of the source VM if that stays inside the library, else first VM dir."""
        sibling = source.path.parent.parent / clone_name
        if path_is_within_any(sibling, self.settings.vm_dirs):
            return sibling
        return Path(self.settings.vm_dirs[0]).expanduser().resolve() / clone_name


def _completed(vm: DiscoveredVm, operation: str, **extra: Any) -> dict[str, Any]:
    return {
        "vm": vm.name,
        "path": str(vm.path),
        "operation": operation,
        "status": "completed",
        **extra,
    }


def _no_change(vm: DiscoveredVm, state: str, operation: str) -> dict[str, Any]:
    return {
        "vm": vm.name,
        "path": str(vm.path),
        "operation": operation,
        "status": "no_change",
        "power_state": state,
        "message": f"{vm.name!r} is already {state}.",
    }


def _remove_if_empty(directory: Path) -> None:
    try:
        if directory.is_dir() and not any(directory.iterdir()):
            shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        logger.debug("Could not tidy directory %s", directory, exc_info=True)


def _rename_display_name(vmx_path: Path, clone_name: str) -> None:
    vmx = load_vmx(vmx_path)
    vmx.set("displayname", clone_name)
    vmx.write()


__all__ = ["CloneType", "PowerAction", "WorkstationClient"]
