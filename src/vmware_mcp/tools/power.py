"""Virtual machine power control."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ..config import PermissionMode
from ..errors import InvalidArgumentError
from ..vsphere import lookup, mappers
from ..vsphere.tasks import run_task
from ._common import ToolContext, mcp_tool

DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)

PowerAction = Literal[
    "power_on",
    "power_off",
    "suspend",
    "reset",
    "shutdown_guest",
    "reboot_guest",
    "standby_guest",
]

#: Actions that need VMware Tools running inside the guest.
_GUEST_ACTIONS = frozenset({"shutdown_guest", "reboot_guest", "standby_guest"})

_TARGET_STATE = {
    "power_on": "poweredOn",
    "power_off": "poweredOff",
    "suspend": "suspended",
    "shutdown_guest": "poweredOff",
}


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=DESTRUCTIVE)
    async def vsphere_change_vm_power_state(
        vm: str,
        action: PowerAction,
        ctx: Context,
        wait: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Change the power state of a virtual machine.

        Prefer the guest actions when VMware Tools is running: ``shutdown_guest``
        and ``reboot_guest`` let the operating system shut down cleanly, whereas
        ``power_off`` and ``reset`` are the equivalent of pulling the plug and
        can lose unwritten data.

        Guest actions complete inside the guest and return as soon as vSphere
        has passed the request to VMware Tools, so ``wait`` does not apply to
        them.

        Requires permission mode ``write`` or higher.

        Args:
            vm: VM name, managed object id, UUID or inventory path.
            action: One of ``power_on``, ``power_off``, ``suspend``, ``reset``,
                ``shutdown_guest``, ``reboot_guest`` or ``standby_guest``.
            wait: Wait for the vSphere task to finish before returning.
            timeout_seconds: Override the default task timeout.
        """
        settings.require(PermissionMode.WRITE, f"vsphere_change_vm_power_state({action})")

        index = await client.path_index()
        record = await client.resolve(
            lookup.VM,
            vm,
            index=index,
            extra_properties=("runtime.powerState", "guest.toolsRunningStatus"),
        )
        current_state = mappers.as_text(record.props.get("runtime.powerState"))
        tools_running = mappers.as_text(record.props.get("guest.toolsRunningStatus"))
        vm_name = record.get("name")

        if action in _GUEST_ACTIONS and tools_running != "guestToolsRunning":
            raise InvalidArgumentError(
                f"{action!r} needs VMware Tools running in {vm_name!r}, but tools report "
                f"{tools_running or 'an unknown state'}. Use 'power_off' or 'reset' to force "
                f"the operation, accepting that the guest will not shut down cleanly."
            )

        target = _TARGET_STATE.get(action)
        if target is not None and current_state == target:
            return {
                "vm": vm_name,
                "moid": record.moid,
                "operation": action,
                "status": "no_change",
                "power_state": current_state,
                "message": f"{vm_name!r} is already {current_state}.",
            }

        moid = record.moid
        result = {"vm": vm_name, "moid": moid, "previous_power_state": current_state}

        if action in _GUEST_ACTIONS:
            await client.call(_guest_action, moid, action)
            return {
                **result,
                "operation": action,
                "status": "requested",
                "waited": False,
                "message": (
                    f"Asked VMware Tools in {vm_name!r} to {action.replace('_', ' ')}; "
                    f"the guest completes this asynchronously."
                ),
            }

        return await run_task(
            client,
            lambda service_instance: _power_task(service_instance, moid, action),
            operation=f"{action} {vm_name}",
            wait=wait,
            timeout=timeout_seconds,
            reporter=ctx,
            result=result,
        )


def _vm_ref(service_instance: vim.ServiceInstance, moid: str) -> Any:
    return lookup.managed_object(service_instance, vim.VirtualMachine, moid)


def _power_task(service_instance: vim.ServiceInstance, moid: str, action: str) -> Any:
    target = _vm_ref(service_instance, moid)
    if action == "power_on":
        return target.PowerOn()
    if action == "power_off":
        return target.PowerOff()
    if action == "suspend":
        return target.Suspend()
    if action == "reset":
        return target.Reset()
    raise InvalidArgumentError(f"Unsupported power action {action!r}.")


def _guest_action(service_instance: vim.ServiceInstance, moid: str, action: str) -> None:
    target = _vm_ref(service_instance, moid)
    if action == "shutdown_guest":
        target.ShutdownGuest()
    elif action == "reboot_guest":
        target.RebootGuest()
    elif action == "standby_guest":
        target.StandbyGuest()
    else:  # pragma: no cover - guarded by the caller
        raise InvalidArgumentError(f"Unsupported guest action {action!r}.")
