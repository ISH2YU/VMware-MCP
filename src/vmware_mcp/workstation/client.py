"""High-level async client for a local VMware Workstation / Fusion / Player."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Literal

from ..config import Settings
from ..errors import InvalidArgumentError, VMwareMCPError
from .discovery import DiscoveredVm, VmInventory, name_matches
from .guest import GuestAuth, GuestOps
from .paths import (
    normalize_path,
    path_is_within_any,
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
        vms = self.inventory.list()
        running = await self.list_running()
        return {
            "backend": "workstation",
            "product": self.settings.product.value,
            "vmrun": str(self.runner.executable()),
            "vmrun_version": version,
            "vm_directories": [str(path) for path in self.settings.vm_dirs],
            "vm_count": len(vms),
            "running_count": len(running),
            "guest_credentials_configured": self.settings.has_guest_credentials,
            "connection": self.settings.describe(),
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
        running_paths = {normalize_path(path) for path in await self.list_running()}
        results = []
        for vm in self.inventory.list():
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
        vm = self.inventory.resolve(identifier)
        vmx = load_vmx(vm.path)
        detail = vmx.summary()
        running = {normalize_path(path) for path in await self.list_running()}
        detail["power_state"] = "poweredOn" if normalize_path(vm.path) in running else "poweredOff"
        detail["tools_state"] = (
            await self.guest.tools_state(vm.path)
            if detail["power_state"] == "poweredOn"
            else "unknown"
        )
        if detail["power_state"] == "poweredOn":
            detail["ip_address"] = await self.guest.get_ip(vm.path)
        else:
            detail["ip_address"] = None
        detail["snapshots"] = await self.list_snapshots(str(vm.path))
        return detail

    def resolve(self, identifier: str) -> DiscoveredVm:
        return self.inventory.resolve(identifier)

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
            if not path_is_within_any(line, self.settings.vm_dirs):
                continue
            paths.append(line)
        return paths

    async def change_power(
        self, identifier: str, action: PowerAction, *, gui: bool = False
    ) -> dict[str, Any]:
        vm = self.inventory.resolve(identifier)
        running = {normalize_path(path) for path in await self.list_running()}
        is_running = normalize_path(vm.path) in running

        soft_stop = action == "stop"
        soft_reset = action == "reset"
        hard_stop = action == "hard_stop"
        hard_reset = action == "hard_reset"

        if action == "start":
            if is_running:
                return _no_change(vm, "poweredOn", action)
            mode = "gui" if gui else "nogui"
            await self.runner.run("start", str(vm.path), mode)
            return {
                "vm": vm.name,
                "path": str(vm.path),
                "operation": action,
                "status": "completed",
                "mode": mode,
            }

        if soft_stop or hard_stop:
            if not is_running:
                return _no_change(vm, "poweredOff", "stop")
            mode = "soft" if soft_stop else "hard"
            await self.runner.run("stop", str(vm.path), mode)
            return {
                "vm": vm.name,
                "path": str(vm.path),
                "operation": "stop",
                "status": "completed",
                "mode": mode,
            }

        if soft_reset or hard_reset:
            if not is_running:
                raise InvalidArgumentError(
                    f"{vm.name!r} is not running; start it before resetting."
                )
            mode = "soft" if soft_reset else "hard"
            await self.runner.run("reset", str(vm.path), mode)
            return {
                "vm": vm.name,
                "path": str(vm.path),
                "operation": "reset",
                "status": "completed",
                "mode": mode,
            }

        if action == "suspend":
            if not is_running:
                raise InvalidArgumentError(f"{vm.name!r} is not running.")
            await self.runner.run("suspend", str(vm.path), "soft")
            return {"vm": vm.name, "path": str(vm.path), "operation": action, "status": "completed"}

        if action == "pause":
            await self.runner.run("pause", str(vm.path))
            return {"vm": vm.name, "path": str(vm.path), "operation": action, "status": "completed"}

        if action == "unpause":
            await self.runner.run("unpause", str(vm.path))
            return {"vm": vm.name, "path": str(vm.path), "operation": action, "status": "completed"}

        raise InvalidArgumentError(f"Unsupported power action {action!r}.")

    # -- snapshots --------------------------------------------------------- #

    async def list_snapshots(self, identifier: str) -> list[dict[str, Any]]:
        vm = self.inventory.resolve(identifier)
        result = await self.runner.run("listSnapshots", str(vm.path), check=False, timeout=60)
        snapshots = []
        for line in result.lines:
            if line.lower().startswith("total snapshots"):
                continue
            snapshots.append({"name": line, "path": line})
        return snapshots

    async def create_snapshot(self, identifier: str, name: str) -> dict[str, Any]:
        vm = self.inventory.resolve(identifier)
        snapshot_name = validate_snapshot_name(name)
        await self.runner.run("snapshot", str(vm.path), snapshot_name)
        return {
            "vm": vm.name,
            "path": str(vm.path),
            "snapshot": snapshot_name,
            "status": "completed",
        }

    async def revert_snapshot(self, identifier: str, snapshot: str) -> dict[str, Any]:
        vm = self.inventory.resolve(identifier)
        snap = validate_snapshot_name(snapshot)
        await self.runner.run("revertToSnapshot", str(vm.path), snap)
        return {"vm": vm.name, "path": str(vm.path), "snapshot": snap, "status": "completed"}

    async def delete_snapshot(
        self, identifier: str, snapshot: str, *, delete_children: bool = False
    ) -> dict[str, Any]:
        vm = self.inventory.resolve(identifier)
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
        source = self.inventory.resolve(identifier)
        clone_name = validate_vm_name(name)
        if clone_type not in ("full", "linked"):
            raise InvalidArgumentError("clone_type must be 'linked' or 'full'.")
        snap = validate_snapshot_name(snapshot) if snapshot else None

        if destination_dir:
            target_dir = Path(destination_dir).expanduser().resolve()
        else:
            target_dir = self._default_clone_dir(source, clone_name)
        if not path_is_within_any(target_dir, self.settings.vm_dirs):
            listed = ", ".join(str(path) for path in self.settings.vm_dirs)
            raise InvalidArgumentError(
                f"Clone destination {target_dir} is outside the configured VM directories "
                f"({listed}). Put clones under VMWARE_VM_DIRS."
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_vmx = target_dir / f"{clone_name}.vmx"
        if target_vmx.exists():
            raise InvalidArgumentError(f"A VM already exists at {target_vmx}.")

        args = [str(source.path), str(target_vmx), clone_type]
        if snap:
            args += [snap]
        await self.runner.run("clone", *args, timeout=max(self.settings.command_timeout, 600))

        # Linked/full clones keep the source display name; rename to what was asked for.
        if target_vmx.is_file():
            vmx = load_vmx(target_vmx)
            vmx.set("displayname", clone_name)
            vmx.write()

        self.inventory.refresh(force=True)
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
    ) -> dict[str, Any]:
        if count < 1:
            raise InvalidArgumentError("count must be at least 1.")
        if count > 50:
            raise InvalidArgumentError("Refusing to clone more than 50 VMs in one call.")
        prefix = validate_vm_name(name_prefix, field="name_prefix")
        created = []
        errors = []
        for index in range(1, count + 1):
            clone_name = f"{prefix}-{index:02d}"
            try:
                result = await self.clone_vm(
                    identifier,
                    clone_name,
                    destination_dir=(
                        str(Path(destination_dir).expanduser() / clone_name)
                        if destination_dir
                        else None
                    ),
                    clone_type=clone_type,
                    snapshot=snapshot,
                )
                if start:
                    await self.change_power(result["path"], "start")
                    result["powered_on"] = True
                created.append(result)
            except VMwareMCPError as exc:
                errors.append({"name": clone_name, "error": str(exc)})
            except Exception as exc:
                logger.exception("Clone %s failed", clone_name)
                errors.append({"name": clone_name, "error": str(exc)})
        return {
            "requested": count,
            "created": len(created),
            "failed": len(errors),
            "vms": created,
            "errors": errors,
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
        vm = self.inventory.resolve(identifier)
        if await self._is_running(vm):
            raise InvalidArgumentError(
                f"{vm.name!r} is powered on. Power it off before changing CPU, memory or name."
            )
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
        self.inventory.refresh(force=True)
        return {"vm": vm.name, "path": str(vm.path), "status": "completed", **changes}

    async def delete_vm(self, identifier: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise InvalidArgumentError(
                "Refusing to delete a VM without confirm=true. This removes the .vmx and its disks."
            )
        vm = self.inventory.resolve(identifier)
        if await self._is_running(vm):
            raise InvalidArgumentError(f"{vm.name!r} is powered on. Stop it before deleting.")
        await self.runner.run("deleteVM", str(vm.path))
        # deleteVM removes registered files; clean the empty directory if left behind.
        parent = vm.path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            shutil.rmtree(parent, ignore_errors=True)
        self.inventory.refresh(force=True)
        return {"vm": vm.name, "path": str(vm.path), "status": "completed"}

    async def screenshot(self, identifier: str, destination: str | None = None) -> dict[str, Any]:
        vm = self.inventory.resolve(identifier)
        if destination:
            target = Path(destination).expanduser().resolve()
        else:
            target = vm.path.parent / f"{vm.path.stem}-screenshot.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.runner.run("captureScreen", str(vm.path), str(target))
        return {
            "vm": vm.name,
            "path": str(vm.path),
            "screenshot": str(target),
            "bytes": target.stat().st_size if target.is_file() else 0,
        }

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
        if not self.settings.vm_dirs:
            raise InvalidArgumentError(
                "No VM directories configured; set VMWARE_VM_DIRS before cloning."
            )
        return Path(self.settings.vm_dirs[0]).expanduser().resolve() / clone_name


def _no_change(vm: DiscoveredVm, state: str, action: str) -> dict[str, Any]:
    return {
        "vm": vm.name,
        "path": str(vm.path),
        "operation": action,
        "status": "no_change",
        "power_state": state,
        "message": f"{vm.name!r} is already {state}.",
    }
