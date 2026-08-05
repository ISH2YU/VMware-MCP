from __future__ import annotations

from pathlib import Path

import pytest

from vmware_mcp.config import PermissionMode, Product, Settings, load_settings
from vmware_mcp.errors import ConfigurationError, PermissionDeniedError


def test_loads_defaults_without_any_environment():
    settings = load_settings({})
    assert settings.permission_mode is PermissionMode.READ_ONLY
    assert settings.product in {Product.WORKSTATION, Product.FUSION}
    assert settings.vm_dirs  # platform defaults
    assert settings.has_guest_credentials is False


def test_workstation_settings_from_env(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_GUEST_USERNAME": "Administrator",
            "VMWARE_GUEST_PASSWORD": "Passw0rd!",
            "VMWARE_PRODUCT": "ws",
            "VMWARE_PERMISSION_MODE": "write",
        }
    )
    assert settings.vm_dirs == (tmp_path,)
    assert settings.guest_username == "Administrator"
    assert settings.guest_password == "Passw0rd!"
    assert settings.product is Product.WORKSTATION
    assert settings.permission_mode is PermissionMode.WRITE
    assert settings.has_guest_credentials is True


def test_describe_hides_guest_password():
    settings = Settings(guest_username="admin", guest_password="secret")
    described = settings.describe()
    assert described["guest_credentials_configured"] is True
    assert "secret" not in repr(described)
    assert "secret" not in repr(settings)


def test_missing_vmrun_path_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not a file"):
        load_settings({"VMWARE_VMRUN_PATH": str(tmp_path / "nope")})


def test_page_size_never_exceeds_max_results(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_MAX_RESULTS": "10",
            "VMWARE_DEFAULT_PAGE_SIZE": "999",
        }
    )
    assert settings.default_page_size == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("read-only", PermissionMode.READ_ONLY),
        ("readonly", PermissionMode.READ_ONLY),
        ("RW", PermissionMode.WRITE),
        ("destructive", PermissionMode.DESTRUCTIVE),
        ("admin", PermissionMode.DESTRUCTIVE),
    ],
)
def test_permission_mode_aliases(raw, expected):
    assert PermissionMode.parse(raw) is expected


def test_unknown_permission_mode_is_rejected():
    with pytest.raises(ConfigurationError, match="Invalid permission mode"):
        PermissionMode.parse("yolo")


def test_permission_modes_are_cumulative():
    assert PermissionMode.DESTRUCTIVE.allows(PermissionMode.WRITE)
    assert PermissionMode.WRITE.allows(PermissionMode.READ_ONLY)
    assert not PermissionMode.WRITE.allows(PermissionMode.DESTRUCTIVE)


def test_require_explains_how_to_enable_the_operation():
    settings = Settings()
    settings.require(PermissionMode.READ_ONLY, "vmware_list_vms")
    with pytest.raises(PermissionDeniedError) as excinfo:
        settings.require(PermissionMode.WRITE, "vmware_clone_vm")
    message = str(excinfo.value)
    assert "vmware_clone_vm" in message
    assert "VMWARE_PERMISSION_MODE=write" in message


def test_product_aliases():
    assert Product.parse("workstation") is Product.WORKSTATION
    assert Product.parse("fusion") is Product.FUSION
    with pytest.raises(ConfigurationError):
        Product.parse("esxi")


def test_rejects_nonsense_integers(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="integer"):
        load_settings({"VMWARE_VM_DIRS": str(tmp_path), "VMWARE_COMMAND_TIMEOUT": "nope"})
    with pytest.raises(ConfigurationError, match=">= 1"):
        load_settings({"VMWARE_VM_DIRS": str(tmp_path), "VMWARE_COMMAND_TIMEOUT": "0"})


def test_blank_guest_password_is_kept(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_GUEST_USERNAME": "user",
            "VMWARE_GUEST_PASSWORD": "",
        }
    )
    assert settings.guest_password == ""
