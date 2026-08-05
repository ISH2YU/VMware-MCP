from __future__ import annotations

import pytest

from vmware_mcp.config import PermissionMode, Settings, load_settings
from vmware_mcp.errors import ConfigurationError, PermissionDeniedError

BASE_ENV = {
    "VMWARE_HOST": "vcenter.lab.local",
    "VMWARE_USERNAME": "svc@vsphere.local",
    "VMWARE_PASSWORD": "hunter2",
}


def test_loads_minimal_environment():
    settings = load_settings(BASE_ENV)
    assert settings.host == "vcenter.lab.local"
    assert settings.port == 443
    assert settings.verify_ssl is True
    assert settings.permission_mode is PermissionMode.READ_ONLY


@pytest.mark.parametrize("missing", ["VMWARE_HOST", "VMWARE_USERNAME", "VMWARE_PASSWORD"])
def test_missing_required_values_are_reported(missing):
    env = {key: value for key, value in BASE_ENV.items() if key != missing}
    with pytest.raises(ConfigurationError, match=missing):
        load_settings(env)


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigurationError):
        load_settings({**BASE_ENV, "VMWARE_HOST": "   "})


def test_alternative_env_names_are_accepted():
    env = {
        "VSPHERE_HOST": "esxi.lab.local",
        "VMWARE_USER": "root",
        "VSPHERE_PASSWORD": "pw",
    }
    settings = load_settings(env)
    assert (settings.host, settings.username, settings.password) == ("esxi.lab.local", "root", "pw")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("no", False), ("true", True), ("YES", True)],
)
def test_verify_ssl_accepts_common_boolean_spellings(value, expected):
    assert load_settings({**BASE_ENV, "VMWARE_VERIFY_SSL": value}).verify_ssl is expected


def test_insecure_flag_overrides_verification():
    settings = load_settings({**BASE_ENV, "VMWARE_VERIFY_SSL": "true", "VMWARE_INSECURE": "1"})
    assert settings.verify_ssl is False


def test_rejects_nonsense_booleans_and_integers():
    with pytest.raises(ConfigurationError, match="boolean"):
        load_settings({**BASE_ENV, "VMWARE_VERIFY_SSL": "maybe"})
    with pytest.raises(ConfigurationError, match="integer"):
        load_settings({**BASE_ENV, "VMWARE_PORT": "https"})
    with pytest.raises(ConfigurationError, match=">= 1"):
        load_settings({**BASE_ENV, "VMWARE_PORT": "0"})


def test_missing_ca_bundle_is_rejected_at_startup(tmp_path):
    with pytest.raises(ConfigurationError, match="missing file"):
        load_settings({**BASE_ENV, "VMWARE_CA_BUNDLE": str(tmp_path / "nope.pem")})

    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    assert load_settings({**BASE_ENV, "VMWARE_CA_BUNDLE": str(bundle)}).ca_bundle == str(bundle)


def test_page_size_never_exceeds_max_results():
    settings = load_settings(
        {**BASE_ENV, "VMWARE_MAX_RESULTS": "10", "VMWARE_DEFAULT_PAGE_SIZE": "999"}
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
    settings = Settings(host="h", username="u", password="p")
    settings.require(PermissionMode.READ_ONLY, "vsphere_list_vms")
    with pytest.raises(PermissionDeniedError) as excinfo:
        settings.require(PermissionMode.WRITE, "vsphere_clone_vm")
    message = str(excinfo.value)
    assert "vsphere_clone_vm" in message
    assert "VMWARE_PERMISSION_MODE=write" in message


def test_describe_omits_the_password():
    settings = Settings(host="h", username="u", password="topsecret")
    described = settings.describe()
    assert "topsecret" not in repr(described)
    assert "topsecret" not in repr(settings)
    assert described["permission_mode"] == "read-only"
