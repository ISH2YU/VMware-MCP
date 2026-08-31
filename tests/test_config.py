"""Environment parsing, permission modes and the redacted view."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from vmware_mcp.config import (
    PermissionMode,
    Product,
    Settings,
    default_host_write_directory,
    default_vm_directories,
    load_settings,
)
from vmware_mcp.errors import ConfigurationError, PermissionDeniedError


def test_defaults_without_any_environment():
    settings = load_settings({})
    assert settings.permission_mode is PermissionMode.READ_ONLY
    assert settings.product in {Product.WORKSTATION, Product.FUSION}
    assert settings.vm_dirs == default_vm_directories()
    assert settings.has_guest_credentials is False
    assert settings.max_concurrency == 4
    assert settings.host_read_dirs == ()


def test_settings_from_environment(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_GUEST_USERNAME": "Administrator",
            "VMWARE_GUEST_PASSWORD": "Passw0rd!",
            "VMWARE_PRODUCT": "ws",
            "VMWARE_PERMISSION_MODE": "write",
            "VMWARE_MAX_CONCURRENCY": "8",
            "VMWARE_CLONE_TIMEOUT": "600",
            "VMWARE_MAX_CLONE_BATCH": "10",
            "VMWARE_GUEST_TEMP_DIR": "D:\\scratch",
        }
    )
    assert settings.vm_dirs == (tmp_path,)
    assert settings.guest_username == "Administrator"
    assert settings.product is Product.WORKSTATION
    assert settings.permission_mode is PermissionMode.WRITE
    assert settings.max_concurrency == 8
    assert settings.clone_timeout == 600
    assert settings.max_clone_batch == 10
    assert settings.guest_temp_dir == "D:\\scratch"


def test_several_vm_directories(tmp_path: Path):
    first, second = tmp_path / "a", tmp_path / "b"
    settings = load_settings({"VMWARE_VM_DIRS": os.pathsep.join([str(first), str(second)])})
    assert settings.vm_dirs == (first, second)


def test_blank_entries_in_a_directory_list_are_dropped(tmp_path: Path):
    raw = os.pathsep.join([str(tmp_path), "", "  "])
    assert load_settings({"VMWARE_VM_DIRS": raw}).vm_dirs == (tmp_path,)


@pytest.mark.parametrize("raw", [os.pathsep, os.pathsep * 3, f" {os.pathsep} "])
def test_a_directory_list_with_no_real_paths_is_rejected(raw):
    """Falling back to defaults here would quietly switch the VM sandbox off."""
    with pytest.raises(ConfigurationError, match="no usable paths"):
        load_settings({"VMWARE_VM_DIRS": raw})


def test_home_is_expanded():
    settings = load_settings({"VMWARE_VM_DIRS": "~/vms"})
    assert settings.vm_dirs == (Path.home() / "vms",)


def test_legacy_aliases_still_work(tmp_path: Path):
    settings = load_settings({"VMWARE_VM_DIR": str(tmp_path), "VMWARE_GUEST_USER": "legacy"})
    assert settings.vm_dirs == (tmp_path,)
    assert settings.guest_username == "legacy"


# --------------------------------------------------------------------------- #
# Host I/O allow-lists
# --------------------------------------------------------------------------- #


def test_host_write_defaults_to_the_library_plus_scratch(tmp_path: Path):
    settings = load_settings({"VMWARE_VM_DIRS": str(tmp_path)})
    assert settings.effective_host_write_dirs() == (tmp_path, default_host_write_directory())


def test_host_write_can_be_narrowed(tmp_path: Path):
    settings = load_settings(
        {"VMWARE_VM_DIRS": str(tmp_path), "VMWARE_HOST_WRITE_DIRS": str(tmp_path / "out")}
    )
    assert settings.effective_host_write_dirs() == (tmp_path / "out",)


def test_a_star_disables_the_allow_list(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_HOST_READ_DIRS": "*",
            "VMWARE_HOST_WRITE_DIRS": "*",
        }
    )
    assert settings.host_read_dirs == ()
    assert settings.host_write_dirs == ()
    assert settings.effective_host_write_dirs() == (tmp_path, default_host_write_directory())


def test_default_scratch_directory_is_under_temp():
    assert default_host_write_directory().parent == Path(tempfile.gettempdir())


# --------------------------------------------------------------------------- #
# Guest temp directory
# --------------------------------------------------------------------------- #


def test_guest_temp_defaults_per_family():
    settings = Settings()
    assert settings.guest_temp("windows") == r"C:\Windows\Temp"
    assert settings.guest_temp("linux") == "/tmp"
    assert settings.guest_temp(None) == r"C:\Windows\Temp"


def test_guest_temp_override_is_used_for_both_families():
    settings = Settings(guest_temp_dir="D:\\scratch\\")
    assert settings.guest_temp("windows") == "D:\\scratch"
    assert settings.guest_temp("linux") == "D:\\scratch"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_describe_hides_the_guest_password():
    settings = Settings(guest_username="admin", guest_password="secret")
    described = settings.describe()
    assert described["guest_credentials_configured"] is True
    assert "secret" not in repr(described)
    assert "secret" not in repr(settings)


def test_describe_hides_the_vmx_password():
    settings = Settings(vmx_password="vmxsecret")
    assert "vmxsecret" not in repr(settings)
    assert "vmxsecret" not in repr(settings.describe())


def test_describe_lists_the_effective_allow_lists(tmp_path: Path):
    described = Settings(vm_dirs=(tmp_path,)).describe()
    assert described["host_read_dirs"] == "unrestricted"
    assert str(tmp_path) in described["host_write_dirs"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_a_bad_vmrun_path_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not a file"):
        load_settings({"VMWARE_VMRUN_PATH": str(tmp_path / "nope")})


def test_a_directory_is_not_a_vmrun_binary(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not a file"):
        load_settings({"VMWARE_VMRUN_PATH": str(tmp_path)})


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
        ("READ_ONLY", PermissionMode.READ_ONLY),
        ("ro", PermissionMode.READ_ONLY),
        ("RW", PermissionMode.WRITE),
        ("read-write", PermissionMode.WRITE),
        ("destructive", PermissionMode.DESTRUCTIVE),
        ("admin", PermissionMode.DESTRUCTIVE),
        ("  full  ", PermissionMode.DESTRUCTIVE),
    ],
)
def test_permission_mode_aliases(raw, expected):
    assert PermissionMode.parse(raw) is expected


def test_unknown_permission_mode_is_rejected():
    with pytest.raises(ConfigurationError, match="Invalid permission mode"):
        PermissionMode.parse("yolo")


def test_permission_modes_are_cumulative():
    assert PermissionMode.DESTRUCTIVE.allows(PermissionMode.WRITE)
    assert PermissionMode.DESTRUCTIVE.allows(PermissionMode.READ_ONLY)
    assert PermissionMode.WRITE.allows(PermissionMode.READ_ONLY)
    assert not PermissionMode.WRITE.allows(PermissionMode.DESTRUCTIVE)
    assert not PermissionMode.READ_ONLY.allows(PermissionMode.WRITE)


def test_require_explains_how_to_enable_the_operation():
    settings = Settings()
    settings.require(PermissionMode.READ_ONLY, "vmware_list_vms")
    with pytest.raises(PermissionDeniedError) as excinfo:
        settings.require(PermissionMode.WRITE, "vmware_clone_vm")
    message = str(excinfo.value)
    assert "vmware_clone_vm" in message
    assert "VMWARE_PERMISSION_MODE=write" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("workstation", Product.WORKSTATION),
        ("WS", Product.WORKSTATION),
        ("fusion", Product.FUSION),
        ("VMware Fusion", Product.FUSION),
        ("player", Product.PLAYER),
        ("workstation player", Product.PLAYER),
    ],
)
def test_product_aliases(raw, expected):
    assert Product.parse(raw) is expected


def test_unknown_product_is_rejected():
    with pytest.raises(ConfigurationError, match="Invalid product"):
        Product.parse("esxi")


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("VMWARE_COMMAND_TIMEOUT", "nope", "integer"),
        ("VMWARE_COMMAND_TIMEOUT", "0", ">= 1"),
        ("VMWARE_MAX_CONCURRENCY", "0", ">= 1"),
        ("VMWARE_MAX_OUTPUT_BYTES", "10", ">= 1024"),
        ("VMWARE_MAX_CLONE_BATCH", "0", ">= 1"),
        ("VMWARE_CACHE_TTL", "-1", ">= 0"),
    ],
)
def test_nonsense_numbers_are_rejected(tmp_path: Path, variable, value, match):
    with pytest.raises(ConfigurationError, match=match):
        load_settings({"VMWARE_VM_DIRS": str(tmp_path), variable: value})


def test_a_blank_guest_password_is_kept(tmp_path: Path):
    settings = load_settings(
        {
            "VMWARE_VM_DIRS": str(tmp_path),
            "VMWARE_GUEST_USERNAME": "user",
            "VMWARE_GUEST_PASSWORD": "",
        }
    )
    assert settings.guest_password == ""
    assert settings.has_guest_credentials is True


def test_whitespace_only_values_fall_back_to_defaults(tmp_path: Path):
    settings = load_settings({"VMWARE_VM_DIRS": str(tmp_path), "VMWARE_LOG_LEVEL": "   "})
    assert settings.log_level == "INFO"


def test_log_level_is_upper_cased(tmp_path: Path):
    settings = load_settings({"VMWARE_VM_DIRS": str(tmp_path), "VMWARE_LOG_LEVEL": "debug"})
    assert settings.log_level == "DEBUG"


def test_settings_are_frozen():
    import dataclasses

    settings = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.permission_mode = PermissionMode.DESTRUCTIVE  # type: ignore[misc]
