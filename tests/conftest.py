"""Shared fixtures: a fake vmrun, a small VM library, and a wired-up server."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_vmrun import FakeVmrun, write_vmx
from vmware_mcp.config import PermissionMode, Product, Settings
from vmware_mcp.server import create_server
from vmware_mcp.workstation.client import WorkstationClient


@pytest.fixture
def vm_root(tmp_path: Path) -> Path:
    """A VM library with one Windows golden image, one Linux VM and one old VM."""
    library = tmp_path / "library"
    write_vmx(library, "win11-golden", guest_os="windows11-64", cpus=4, memory_mb=8192)
    write_vmx(library, "ubuntu-dev", guest_os="ubuntu-64", cpus=2, memory_mb=2048)
    write_vmx(library, "win10-legacy", guest_os="windows9-64", cpus=2, memory_mb=4096)
    return library


@pytest.fixture
def outside_dir(tmp_path: Path) -> Path:
    """A directory deliberately NOT listed in VMWARE_VM_DIRS."""
    path = tmp_path / "outside"
    path.mkdir()
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeVmrun:
    return FakeVmrun(executable_path=tmp_path / "vmrun")


@pytest.fixture
def settings(vm_root: Path) -> Settings:
    return Settings(
        vm_dirs=(vm_root,),
        product=Product.WORKSTATION,
        permission_mode=PermissionMode.DESTRUCTIVE,
        guest_username="Administrator",
        guest_password="Passw0rd!",
        cache_ttl=0,
    )


@pytest.fixture
def client(settings: Settings, fake: FakeVmrun) -> WorkstationClient:
    return WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]


@pytest.fixture
def server(client: WorkstationClient, settings: Settings):
    return create_server(settings, client=client)


@pytest.fixture
def golden(vm_root: Path) -> str:
    return str(vm_root / "win11-golden" / "win11-golden.vmx")


@pytest.fixture
def ubuntu(vm_root: Path) -> str:
    return str(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")


def make_client(
    vm_root: Path,
    fake: FakeVmrun,
    *,
    mode: PermissionMode = PermissionMode.DESTRUCTIVE,
    **overrides,
) -> WorkstationClient:
    """A client with tweaked settings, for tests that need a different mode."""
    settings = Settings(
        vm_dirs=(vm_root,),
        product=Product.WORKSTATION,
        permission_mode=mode,
        guest_username="Administrator",
        guest_password="Passw0rd!",
        cache_ttl=0,
        **overrides,
    )
    return WorkstationClient(settings, runner=fake)  # type: ignore[arg-type]
