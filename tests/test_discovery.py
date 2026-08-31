"""Walking the VM library and resolving user-supplied identifiers."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_vmrun import write_vmx
from vmware_mcp.errors import AmbiguousObjectError, ObjectNotFoundError
from vmware_mcp.workstation.discovery import VmInventory, discover_vmx_files, name_matches


def test_discovers_every_vmx(vm_root: Path):
    found = discover_vmx_files((vm_root,))
    assert [path.stem for path in found] == ["ubuntu-dev", "win10-legacy", "win11-golden"]


def test_missing_directories_are_skipped(tmp_path: Path, vm_root: Path):
    found = discover_vmx_files((vm_root, tmp_path / "does-not-exist"))
    assert len(found) == 3


def test_lock_directories_and_dotfiles_are_ignored(vm_root: Path):
    lock = vm_root / "win11-golden" / "win11-golden.vmx.lck"
    lock.mkdir()
    (lock / "stale.vmx").write_text('displayName = "stale"\n')
    hidden = vm_root / ".Trash"
    hidden.mkdir()
    (hidden / "deleted.vmx").write_text('displayName = "deleted"\n')
    names = [path.stem for path in discover_vmx_files((vm_root,))]
    assert "stale" not in names
    assert "deleted" not in names


def test_depth_is_capped(tmp_path: Path):
    deep = tmp_path
    for level in range(6):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (deep / "buried.vmx").write_text('displayName = "buried"\n')
    assert discover_vmx_files((tmp_path,), max_depth=2) == []
    assert len(discover_vmx_files((tmp_path,), max_depth=8)) == 1


def test_a_symlink_cycle_does_not_hang(tmp_path: Path):
    library = tmp_path / "library"
    nested = library / "nested"
    nested.mkdir(parents=True)
    write_vmx(nested, "real")
    try:
        (nested / "loop").symlink_to(library, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    found = discover_vmx_files((library,), max_depth=10)
    assert [path.stem for path in found] == ["real"]


def test_a_symlinked_vmx_pointing_outside_is_not_registered(tmp_path: Path):
    """A link inside the library must not smuggle in a VM that lives elsewhere."""
    library = tmp_path / "library"
    elsewhere = tmp_path / "elsewhere"
    library.mkdir()
    real = write_vmx(elsewhere, "smuggled")
    try:
        (library / "smuggled.vmx").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    assert discover_vmx_files((library,)) == []


def test_a_symlinked_directory_pointing_outside_is_not_registered(tmp_path: Path):
    library = tmp_path / "library"
    elsewhere = tmp_path / "elsewhere"
    library.mkdir()
    write_vmx(elsewhere, "smuggled")
    try:
        (library / "link").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    assert discover_vmx_files((library,)) == []


def test_a_symlinked_vmx_inside_the_library_is_fine(tmp_path: Path):
    library = tmp_path / "library"
    real = write_vmx(library, "genuine")
    try:
        (library / "alias.vmx").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    # The alias resolves to the real file, which is inside the library, so it is
    # kept exactly once.
    assert [path.stem for path in discover_vmx_files((library,))] == ["genuine"]


def test_a_second_configured_root_is_still_allowed(tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    real = write_vmx(second, "shared")
    try:
        (first / "shared.vmx").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    assert [path.stem for path in discover_vmx_files((first, second))] == ["shared"]


def test_the_same_vm_reached_twice_appears_once(tmp_path: Path, vm_root: Path):
    found = discover_vmx_files((vm_root, vm_root, vm_root.parent / "library"))
    assert len(found) == 3


def test_max_files_stops_the_walk(tmp_path: Path):
    for index in range(6):
        write_vmx(tmp_path, f"vm{index}")
    assert len(discover_vmx_files((tmp_path,), max_files=3)) == 3


def test_unreadable_vmx_is_skipped_not_fatal(vm_root: Path, caplog):
    broken = vm_root / "broken"
    broken.mkdir()
    (broken / "broken.vmx").write_bytes(b"\xff\xfe not really a vmx")
    inventory = VmInventory((vm_root,), ttl=0)
    names = {vm.name for vm in inventory.list()}
    # It parses as an empty config rather than exploding, and the good VMs survive.
    assert {"win11-golden", "ubuntu-dev", "win10-legacy"} <= names


def test_resolution_by_name_stem_folder_and_uuid(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    by_name = inventory.resolve("win11-golden")
    assert inventory.resolve(str(by_name.path)).path == by_name.path
    assert inventory.resolve("WIN11-GOLDEN").path == by_name.path
    assert inventory.resolve(by_name.uuid or "").path == by_name.path


def test_resolution_is_case_insensitive_on_display_name(tmp_path: Path):
    library = tmp_path / "library"
    write_vmx(library, "folder-name", display_name="Pretty Name")
    inventory = VmInventory((library,), ttl=0)
    assert inventory.resolve("pretty name").name == "Pretty Name"
    assert inventory.resolve("folder-name").name == "Pretty Name"


def test_ambiguous_names_list_the_candidates(vm_root: Path):
    write_vmx(vm_root / "other", "win11-golden", guest_os="windows11-64")
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(AmbiguousObjectError) as excinfo:
        inventory.resolve("win11-golden")
    message = str(excinfo.value)
    assert "2 VMs match" in message
    assert ".vmx" in message


def test_unknown_identifier_lists_what_exists(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(ObjectNotFoundError) as excinfo:
        inventory.resolve("nope")
    assert "win11-golden" in str(excinfo.value)


def test_empty_identifier_is_rejected(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(ObjectNotFoundError, match="must not be empty"):
        inventory.resolve("   ")


def test_a_vmx_outside_the_library_is_refused(tmp_path: Path, vm_root: Path):
    sneaky = write_vmx(tmp_path / "outside", "sneaky")
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(ObjectNotFoundError, match="outside"):
        inventory.resolve(str(sneaky))


def test_a_missing_vmx_inside_the_library_says_so(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    with pytest.raises(ObjectNotFoundError, match=r"No \.vmx file at"):
        inventory.resolve(str(vm_root / "ghost" / "ghost.vmx"))


def test_cache_is_reused_until_invalidated(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=300)
    assert len(inventory.list()) == 3
    write_vmx(vm_root, "late-arrival")
    assert len(inventory.list()) == 3, "cache should still be warm"
    inventory.invalidate()
    assert len(inventory.list()) == 4


def test_forced_refresh_bypasses_the_cache(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=300)
    inventory.list()
    write_vmx(vm_root, "forced")
    assert len(inventory.refresh(force=True)) == 4


async def test_async_refresh_matches_sync(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=0)
    assert {vm.name for vm in await inventory.list_async()} == {vm.name for vm in inventory.list()}
    resolved = await inventory.resolve_async("ubuntu-dev")
    assert resolved.guest_os_family == "linux"


async def test_invalidating_during_a_scan_does_not_publish_stale_data(vm_root: Path):
    """A delete that lands mid-scan must not be papered over for a whole TTL."""
    inventory = VmInventory((vm_root,), ttl=300)
    real_scan = inventory._scan
    calls = {"count": 0}

    def scan_then_change():
        calls["count"] += 1
        result = real_scan()
        if calls["count"] == 1:
            # Simulate another task deleting a VM while this scan was running.
            inventory.invalidate()
        return result

    inventory._scan = scan_then_change
    await inventory.refresh_async()
    assert calls["count"] == 2, "the scan should have been repeated after the change"


async def test_a_library_that_keeps_changing_is_not_cached_as_fresh(vm_root: Path):
    inventory = VmInventory((vm_root,), ttl=300)
    real_scan = inventory._scan

    def always_changing():
        result = real_scan()
        inventory.invalidate()
        return result

    inventory._scan = always_changing
    await inventory.refresh_async()
    assert not inventory._is_fresh(), "a contested scan must not be trusted"


def test_an_empty_library_still_caches(tmp_path: Path):
    inventory = VmInventory((tmp_path,), ttl=300)
    assert inventory.list() == []
    write_vmx(tmp_path, "appeared")
    assert inventory.list() == [], "an empty result should be cached too"


@pytest.mark.parametrize(
    ("value", "pattern", "expected"),
    [
        ("web-test-01", "web", True),
        ("web-test-01", "WEB-TEST", True),
        ("web-test-01", "web-*", True),
        ("web-test-01", "*-01", True),
        ("web-test-01", "web-test-0?", True),
        ("web-test-01", "db", False),
        ("web-test-01", "web-*-99", False),
        ("web-test-01", None, True),
        (None, "web", False),
    ],
)
def test_name_matching(value, pattern, expected):
    assert name_matches(value, pattern) is expected
