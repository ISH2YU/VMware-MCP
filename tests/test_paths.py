"""The two sandboxes: the VM library, and host read/write allow-lists."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vmware_mcp.errors import InvalidArgumentError, ObjectNotFoundError
from vmware_mcp.workstation.paths import (
    MAX_NAME_LENGTH,
    normalize_path,
    path_is_within,
    path_is_within_any,
    require_host_read,
    require_host_write,
    require_within_vm_dirs,
    validate_guest_path,
    validate_snapshot_name,
    validate_vm_name,
)


def test_path_is_within_matches_self_and_descendants(tmp_path: Path):
    child = tmp_path / "a" / "b.vmx"
    child.parent.mkdir()
    child.write_text("x")
    assert path_is_within(child, tmp_path)
    assert path_is_within(tmp_path, tmp_path)
    assert not path_is_within(tmp_path, child)


def test_path_is_within_is_not_fooled_by_a_shared_prefix(tmp_path: Path):
    library = tmp_path / "vms"
    sibling = tmp_path / "vms-backup"
    library.mkdir()
    sibling.mkdir()
    assert not path_is_within(sibling / "x.vmx", library)


def test_path_is_within_resolves_symlinks(tmp_path: Path):
    library = tmp_path / "library"
    secret = tmp_path / "secret"
    library.mkdir()
    secret.mkdir()
    (secret / "hidden.vmx").write_text("x")
    link = library / "link.vmx"
    try:
        link.symlink_to(secret / "hidden.vmx")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    # The link lives in the library but its target does not, and resolving wins.
    assert not path_is_within(link, library)


def test_path_is_within_any(tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert path_is_within_any(second / "vm.vmx", (first, second))
    assert not path_is_within_any(tmp_path / "three" / "vm.vmx", (first, second))


def test_normalize_path_is_absolute(tmp_path: Path):
    assert Path(normalize_path(tmp_path / "x")).is_absolute()


@pytest.mark.skipif(sys.platform != "win32", reason="case folding is Windows-only")
def test_normalize_path_folds_case_on_windows(tmp_path: Path):
    assert normalize_path(str(tmp_path).upper()) == normalize_path(str(tmp_path).lower())


def test_require_within_vm_dirs_reports_the_configured_roots(tmp_path: Path):
    library = tmp_path / "library"
    library.mkdir()
    with pytest.raises(ObjectNotFoundError) as excinfo:
        require_within_vm_dirs(tmp_path / "elsewhere.vmx", (library,), what="VM")
    message = str(excinfo.value)
    assert "outside" in message
    assert str(library) in message
    assert "VMWARE_VM_DIRS" in message


def test_host_read_is_unrestricted_when_no_dirs_configured(tmp_path: Path):
    assert require_host_read(tmp_path / "anything.iso", ()) == (tmp_path / "anything.iso")


def test_host_read_is_enforced_when_configured(tmp_path: Path):
    allowed = tmp_path / "builds"
    allowed.mkdir()
    assert require_host_read(allowed / "app.msi", (allowed,)) == allowed / "app.msi"
    with pytest.raises(InvalidArgumentError, match="VMWARE_HOST_READ_DIRS"):
        require_host_read(tmp_path / "secrets" / "id_rsa", (allowed,))


def test_host_write_is_enforced_when_configured(tmp_path: Path):
    allowed = tmp_path / "out"
    allowed.mkdir()
    assert require_host_write(allowed / "log.txt", (allowed,)) == allowed / "log.txt"
    with pytest.raises(InvalidArgumentError, match="VMWARE_HOST_WRITE_DIRS"):
        require_host_write(tmp_path / "etc" / "passwd", (allowed,))


@pytest.mark.parametrize("name", ["win11-test-01", "Lab VM", "build_2026.1"])
def test_valid_vm_names(name):
    assert validate_vm_name(name) == name.strip()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "..",
        ".",
        ".hidden",
        "../escape",
        "foo/bar",
        "foo\\bar",
        "with:colon",
        'quote"name',
        "pipe|name",
        "star*name",
        "trailing.",
        "CON",
        "lpt1.txt",
        "x" * (MAX_NAME_LENGTH + 1),
    ],
)
def test_rejected_vm_names(name):
    with pytest.raises(InvalidArgumentError):
        validate_vm_name(name)


def test_snapshot_names_allow_punctuation_but_not_paths():
    assert validate_snapshot_name("before install (v2)") == "before install (v2)"
    for bad in ["", "   ", "a/b", "a\\b", "../x", "x" * (MAX_NAME_LENGTH + 1)]:
        with pytest.raises(InvalidArgumentError):
            validate_snapshot_name(bad)


def test_guest_paths_reject_empty_and_null():
    assert validate_guest_path("  C:\\Temp\\a.txt ") == "C:\\Temp\\a.txt"
    with pytest.raises(InvalidArgumentError):
        validate_guest_path("")
    with pytest.raises(InvalidArgumentError):
        validate_guest_path("C:\\a\x00b")
