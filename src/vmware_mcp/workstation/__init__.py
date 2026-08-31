"""Local VMware Workstation / Fusion / Player backend."""

from .client import WorkstationClient
from .discovery import DiscoveredVm, VmInventory
from .guest import GuestCommandResult, GuestOps
from .vmrun import GuestAuth, VmrunRunner, find_vmrun

__all__ = [
    "DiscoveredVm",
    "GuestAuth",
    "GuestCommandResult",
    "GuestOps",
    "VmInventory",
    "VmrunRunner",
    "WorkstationClient",
    "find_vmrun",
]
