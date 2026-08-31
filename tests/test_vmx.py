"""Reading, editing and rewriting .vmx files."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_vmrun import write_vmx
from vmware_mcp.errors import InvalidArgumentError, ObjectNotFoundError
from vmware_mcp.workstation.vmx import (
    apply_config_changes,
    guest_os_family,
    load_vmx,
)


def test_summary_extracts_the_interesting_fields(vm_root: Path):
    summary = load_vmx(vm_root / "win11-golden" / "win11-golden.vmx").summary()
    assert summary["name"] == "win11-golden"
    assert summary["guest_os"] == "windows11-64"
    assert summary["guest_os_family"] == "windows"
    assert summary["cpu_count"] == 4
    assert summary["memory_mb"] == 8192
    assert summary["memory_gib"] == 8.0
    assert summary["firmware"] == "efi"
    assert summary["ethernet"][0]["connection_type"] == "nat"
    assert summary["ethernet"][0]["mac_address"] == "00:0c:29:aa:bb:cc"
    assert summary["disks"][0]["file"] == "win11-golden.vmdk"


def test_a_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(ObjectNotFoundError, match=r"No \.vmx file"):
        load_vmx(tmp_path / "ghost.vmx")


def test_keys_are_case_insensitive(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('DisplayName = "Mixed Case"\nMEMSIZE = "2048"\n')
    vmx = load_vmx(path)
    assert vmx.get("displayname") == "Mixed Case"
    assert vmx.get("DISPLAYNAME") == "Mixed Case"
    assert vmx.summary()["memory_mb"] == 2048


def test_comments_and_blank_lines_are_ignored(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('# a comment\n\ndisplayName = "Kept"\n\n')
    assert load_vmx(path).get("displayname") == "Kept"


def test_unquoted_values_are_accepted(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text("numvcpus = 4\n")
    assert load_vmx(path).summary()["cpu_count"] == 4


def test_a_utf8_bom_is_handled(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_bytes('\ufeffdisplayName = "BOM"\n'.encode())
    assert load_vmx(path).get("displayname") == "BOM"


def test_an_unknown_encoding_falls_back(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('.encoding = "not-a-real-codec"\ndisplayName = "Fallback"\n')
    assert load_vmx(path).get("displayname") == "Fallback"


def test_nonsense_numbers_become_none(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('memsize = "lots"\n')
    assert load_vmx(path).summary()["memory_mb"] is None


def test_writing_preserves_key_order(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('displayName = "One"\nguestOS = "windows11-64"\nmemsize = "1024"\n')
    vmx = load_vmx(path)
    vmx.set("memsize", 2048)
    vmx.write()
    body = path.read_text().splitlines()
    assert body[0].startswith(".encoding")
    assert [line.split(" =")[0] for line in body[1:]] == ["displayname", "guestos", "memsize"]


def test_writing_is_atomic_and_leaves_no_temp_file(vm_root: Path):
    path = vm_root / "ubuntu-dev" / "ubuntu-dev.vmx"
    vmx = load_vmx(path)
    vmx.set("annotation", "updated")
    vmx.write()
    assert load_vmx(path).get("annotation") == "updated"
    assert list(path.parent.glob("*.tmp")) == []


def test_a_failed_write_does_not_destroy_the_original(vm_root: Path, monkeypatch):
    path = vm_root / "ubuntu-dev" / "ubuntu-dev.vmx"
    original = path.read_text()
    vmx = load_vmx(path)
    vmx.set("memsize", 9999)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        vmx.write()
    monkeypatch.undo()
    assert path.read_text() == original
    assert list(path.parent.glob("*.tmp")) == []


def test_quotes_and_backslashes_survive_a_round_trip(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('displayName = "plain"\n')
    vmx = load_vmx(path)
    vmx.set("annotation", 'He said "hi" \\ then left')
    vmx.write()
    assert load_vmx(path).get("annotation") == 'He said "hi" \\ then left'


def test_deleting_a_key(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('displayName = "x"\nannotation = "note"\n')
    vmx = load_vmx(path)
    vmx.delete("annotation")
    vmx.write()
    assert load_vmx(path).get("annotation") is None


def test_booleans_render_the_way_vmware_writes_them(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text('displayName = "x"\n')
    vmx = load_vmx(path)
    vmx.set("ethernet0.present", True)
    vmx.set("ethernet1.present", False)
    vmx.write()
    body = path.read_text()
    assert 'ethernet0.present = "TRUE"' in body
    assert 'ethernet1.present = "FALSE"' in body


@pytest.mark.parametrize(
    ("guest_os", "family"),
    [
        ("windows11-64", "windows"),
        ("winXPPro", "windows"),
        ("ubuntu-64", "linux"),
        ("rhel9-64", "linux"),
        ("otherlinux-64", "linux"),
        ("darwin22-64", "macos"),
        ("freebsd-64", "bsd"),
        ("solaris11-64", "solaris"),
        ("someOtherOs", "other"),
        (None, None),
        ("", None),
    ],
)
def test_guest_os_family_detection(guest_os, family):
    assert guest_os_family(guest_os) == family


def test_apply_changes_reports_before_and_after(vm_root: Path):
    vmx = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")
    changes = apply_config_changes(vmx, name="ubuntu-ci", cpu_count=8, memory_mb=16384)
    assert changes["previous"]["cpu_count"] == 2
    assert changes["current"]["cpu_count"] == 8
    assert changes["current"]["name"] == "ubuntu-ci"


def test_only_supplied_fields_change(vm_root: Path):
    vmx = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")
    changes = apply_config_changes(vmx, annotation="just a note")
    assert changes["current"]["cpu_count"] == changes["previous"]["cpu_count"]
    assert changes["current"]["annotation"] == "just a note"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpu_count": 0},
        {"cpu_count": -1},
        {"cpu_count": 999},
        {"memory_mb": 1},
        {"memory_mb": 99_999_999},
        {"cores_per_socket": 0},
        {"cpu_count": 6, "cores_per_socket": 4},
    ],
)
def test_invalid_hardware_is_rejected(vm_root: Path, kwargs):
    vmx = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")
    with pytest.raises(InvalidArgumentError):
        apply_config_changes(vmx, **kwargs)


def test_cores_per_socket_checks_against_the_existing_cpu_count(vm_root: Path):
    vmx = load_vmx(vm_root / "ubuntu-dev" / "ubuntu-dev.vmx")  # 2 vCPUs
    apply_config_changes(vmx, cores_per_socket=2)
    with pytest.raises(InvalidArgumentError, match="multiple"):
        apply_config_changes(vmx, cores_per_socket=4)


def test_disks_resolve_relative_filenames(tmp_path: Path):
    path = write_vmx(tmp_path, "disks")
    summary = load_vmx(path).summary()
    assert Path(summary["disks"][0]["path"]) == (path.parent / "disks.vmdk").resolve()


def test_absent_adapters_are_not_listed(tmp_path: Path):
    path = tmp_path / "x.vmx"
    path.write_text(
        'displayName = "x"\n'
        'ethernet0.present = "TRUE"\n'
        'ethernet1.present = "FALSE"\n'
        'ethernet1.connectionType = "bridged"\n'
    )
    adapters = load_vmx(path).summary()["ethernet"]
    assert [adapter["index"] for adapter in adapters] == [0]
