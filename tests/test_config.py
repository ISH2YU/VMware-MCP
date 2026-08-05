from __future__ import annotations

import pytest

from vmware_mcp.config import (
    Backend,
    PermissionMode,
    Product,
    VSphereSettings,
    WorkstationSettings,
    detect_backend,
    load_settings,
    load_vsphere_settings,
    load_workstation_settings,
)
from vmware_mcp.errors import ConfigurationError, PermissionDeniedError

VSPHERE_ENV = {
    "VMWARE_HOST": "vcenter.lab.local",
    "VMWARE_USERNAME": "svc@vsphere.local",
    "VMWARE_PASSWORD": "hunter2",
}


def test_default_backend_is_workstation_without_a_host():
    assert detect_backend({}) is Backend.WORKSTATION
    settings = load_settings({})
    assert isinstance(settings, WorkstationSettings)
    assert settings.backend is Backend.WORKSTATION


def test_a_host_selects_the_vsphere_backend():
    assert detect_backend(VSPHERE_ENV) is Backend.VSPHERE
    settings = load_settings(VSPHERE_ENV)
    assert isinstance(settings, VSphereSettings)


def test_explicit_backend_wins_over_host_detection():
    env = {**VSPHERE_ENV, "VMWARE_BACKEND": "workstation"}
    assert detect_backend(env) is Backend.WORKSTATION


def test_loads_minimal_vsphere_environment():
    settings = load_vsphere_settings(VSPHERE_ENV)
    assert settings.host == "vcenter.lab.local"
    assert settings.port == 443
    assert settings.verify_ssl is True
    assert settings.permission_mode is PermissionMode.READ_ONLY


@pytest.mark.parametrize("missing", ["VMWARE_HOST", "VMWARE_USERNAME", "VMWARE_PASSWORD"])
def test_missing_vsphere_values_are_reported(missing):
    env = {key: value for key, value in VSPHERE_ENV.items() if key != missing}
    with pytest.raises(ConfigurationError, match=missing):
        load_vsphere_settings(env)


def test_blank_host_counts_as_missing():
    with pytest.raises(ConfigurationError):
        load_vsphere_settings({**VSPHERE_ENV, "VMWARE_HOST": "   "})


def test_alternative_env_names_are_accepted():
    env = {
        "VSPHERE_HOST": "esxi.lab.local",
        "VMWARE_USER": "root",
        "VSPHERE_PASSWORD": "pw",
    }
    settings = load_vsphere_settings(env)
    assert (settings.host, settings.username, settings.password) == ("esxi.lab.local", "root", "pw")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("no", False), ("true", True), ("YES", True)],
)
def test_verify_ssl_accepts_common_boolean_spellings(value, expected):
    assert load_vsphere_settings({**VSPHERE_ENV, "VMWARE_VERIFY_SSL": value}).verify_ssl is expected


def test_insecure_flag_overrides_verification():
    settings = load_vsphere_settings(
        {**VSPHERE_ENV, "VMWARE_VERIFY_SSL": "true", "VMWARE_INSECURE": "1"}
    )
    assert settings.verify_ssl is False


def test_rejects_nonsense_booleans_and_integers():
    with pytest.raises(ConfigurationError, match="boolean"):
        load_vsphere_settings({**VSPHERE_ENV, "VMWARE_VERIFY_SSL": "maybe"})
    with pytest.raises(ConfigurationError, match="integer"):
        load_vsphere_settings({**VSPHERE_ENV, "VMWARE_PORT": "https"})
    with pytest.raises(ConfigurationError, match=">= 1"):
        load_vsphere_settings({**VSPHERE_ENV, "VMWARE_PORT": "0"})


def test_missing_ca_bundle_is_rejected_at_startup(tmp_path):
    with pytest.raises(ConfigurationError, match="missing file"):
        load_vsphere_settings({**VSPHERE_ENV, "VMWARE_CA_BUNDLE": str(tmp_path / "nope.pem")})

    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    assert load_vsphere_settings({**VSPHERE_ENV, "VMWARE_CA_BUNDLE": str(bundle)}).ca_bundle == str(
        bundle
    )


def test_page_size_never_exceeds_max_results():
    settings = load_vsphere_settings(
        {**VSPHERE_ENV, "VMWARE_MAX_RESULTS": "10", "VMWARE_DEFAULT_PAGE_SIZE": "999"}
    )
    assert settings.default_page_size == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("read-only", PermissionMode.READ_ONLY),
        ("readonly", PermissionMode.READ_ONLY),
        ("RW", PermissionMode.WRITE),
        ("read_write", PermissionMode.WRITE),
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
    assert not PermissionMode.READ_ONLY.allows(PermissionMode.WRITE)


def test_require_explains_how_to_enable_the_operation():
    settings = WorkstationSettings()
    settings.require(PermissionMode.READ_ONLY, "vmware_list_vms")
    with pytest.raises(PermissionDeniedError) as excinfo:
        settings.require(PermissionMode.WRITE, "vmware_clone_vm")
    message = str(excinfo.value)
    assert "vmware_clone_vm" in message
    assert "VMWARE_PERMISSION_MODE=write" in message


def test_describe_omits_the_password():
    settings = VSphereSettings(host="h", username="u", password="topsecret")
    described = settings.describe()
    assert "topsecret" not in repr(described)
    assert "topsecret" not in repr(settings)
    assert described["permission_mode"] == "read-only"
    assert described["backend"] == "vsphere"


def test_workstation_settings_from_env(tmp_path):
    settings = load_workstation_settings(
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


def test_workstation_describe_hides_guest_password():
    settings = WorkstationSettings(guest_username="admin", guest_password="secret")
    described = settings.describe()
    assert described["guest_credentials_configured"] is True
    assert "secret" not in repr(described)
    assert "secret" not in repr(settings)


def test_missing_vmrun_path_is_rejected(tmp_path):
    with pytest.raises(ConfigurationError, match="not a file"):
        load_workstation_settings({"VMWARE_VMRUN_PATH": str(tmp_path / "nope")})


def test_product_aliases():
    assert Product.parse("workstation") is Product.WORKSTATION
    assert Product.parse("fusion") is Product.FUSION
    with pytest.raises(ConfigurationError):
        Product.parse("esxi")


def test_backend_aliases():
    assert Backend.parse("local") is Backend.WORKSTATION
    assert Backend.parse("vcenter") is Backend.VSPHERE
    with pytest.raises(ConfigurationError):
        Backend.parse("xen")
