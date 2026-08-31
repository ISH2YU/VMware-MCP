"""Path sandbox and name validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_vmrun import write_vmx
from vmware_mcp.errors import InvalidArgumentError, ObjectNotFoundError
from vmware_mcp.workstation.discovery import VmInventory
from vmware_mcp.workstation.guest import _posix_capture_script, _ps_quote, _windows_capture_script
from vmware_mcp.workstation.paths import (
    path_is_within,
    path_is_within_any,
    validate_snapshot_name,
    validate_vm_name,
)


def test_path_is_within_descendant(tmp_path: Path):
    child = tmp_path / "a" / "b.vmx"
    child.parent.mkdir()
    child.write_text("x")
    assert path_is_within(child, tmp_path)
    assert path_is_within(tmp_path, tmp_path)
    assert not path_is_within(tmp_path, child)
    assert not path_is_within_any(tmp_path / "nope", (tmp_path / "other",))


def test_validate_vm_name_blocks_traversal():
    assert validate_vm_name("win11-test-01") == "win11-test-01"
    assert validate_vm_name("  Lab VM  ") == "Lab VM"
    with pytest.raises(InvalidArgumentError):
        validate_vm_name("../escape")
    with pytest.raises(InvalidArgumentError):
        validate_vm_name("foo/bar")
    with pytest.raises(InvalidArgumentError):
        validate_vm_name("foo\\bar")
    with pytest.raises(InvalidArgumentError):
        validate_vm_name("")
    with pytest.raises(InvalidArgumentError):
        validate_vm_name("..")


def test_validate_snapshot_name_blocks_paths():
    assert validate_snapshot_name("golden") == "golden"
    assert validate_snapshot_name("before install") == "before install"
    with pytest.raises(InvalidArgumentError):
        validate_snapshot_name("a/b")
    with pytest.raises(InvalidArgumentError):
        validate_snapshot_name("")


def test_posix_capture_script_quotes_metacharacters():
    script = _posix_capture_script(
        "/bin/echo",
        "hello; rm -rf /",
        "/tmp/out.txt",
        "/tmp/err.txt",
        "/tmp/code.txt",
    )
    assert "'hello;'" in script
    # The semicolon must not be a shell operator in the wrapper.
    assert "echo hello; rm" not in script


def test_windows_capture_script_uses_powershell_literals():
    script = _windows_capture_script(
        r"C:\Tools\app.exe",
        r"/C echo hi & del C:\evil",
        r"C:\Windows\Temp\out.txt",
        r"C:\Windows\Temp\err.txt",
        r"C:\Windows\Temp\code.txt",
    )
    assert "ProcessStartInfo" in script
    assert _ps_quote(r"/C echo hi & del C:\evil") in script
    assert "cmd.exe /C" not in script


def test_inventory_refuses_vmx_outside_configured_dirs(tmp_path: Path):
    library = tmp_path / "library"
    other = tmp_path / "other"
    write_vmx(library, "allowed")
    sneaky = write_vmx(other, "sneaky")
    inventory = VmInventory((library,), ttl=0)
    assert inventory.resolve("allowed").name == "allowed"
    with pytest.raises(ObjectNotFoundError, match="outside"):
        inventory.resolve(str(sneaky))
