"""Server metadata, resources and prompts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from vmware_mcp import __version__
from vmware_mcp.config import PermissionMode, Product, Settings
from vmware_mcp.server import SERVER_NAME, build_instructions, create_server
from vmware_mcp.tools import MODULES


async def test_server_identifies_itself(server):
    async with Client(server) as client:
        assert client.server_info.name == SERVER_NAME
        assert client.server_info.version == __version__
        assert "Workstation" in (client.server_info.title or "")


async def test_instructions_describe_the_workflow(server):
    async with Client(server) as client:
        instructions = client.instructions or ""
    assert "vmware_clone_many" in instructions
    assert "untrusted" in instructions


def test_tool_modules_are_unique():
    assert len(MODULES) == len({module.__name__ for module in MODULES})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (PermissionMode.READ_ONLY, "READ-ONLY"),
        (PermissionMode.WRITE, "may change VMs"),
        (PermissionMode.DESTRUCTIVE, "FULL access"),
    ],
)
def test_instructions_state_what_the_server_may_do(mode, expected, tmp_path: Path):
    settings = Settings(vm_dirs=(tmp_path,), permission_mode=mode, product=Product.WORKSTATION)
    instructions = build_instructions(settings)
    assert expected in instructions
    assert str(tmp_path) in instructions


def test_instructions_mention_missing_credentials(tmp_path: Path):
    settings = Settings(vm_dirs=(tmp_path,), product=Product.WORKSTATION)
    assert "VMWARE_GUEST_USERNAME" in build_instructions(settings)


def test_instructions_name_the_configured_user(tmp_path: Path):
    settings = Settings(
        vm_dirs=(tmp_path,), product=Product.WORKSTATION, guest_username="Administrator"
    )
    assert "Administrator" in build_instructions(settings)


def test_instructions_survive_an_empty_library():
    assert "(none configured)" in build_instructions(Settings())


async def test_vm_resources(server):
    async with Client(server) as client:
        resources = [str(r.uri) for r in (await client.list_resources()).resources]
        assert resources == ["vmware://vms"]
        templates = [
            t.uri_template for t in (await client.list_resource_templates()).resource_templates
        ]
        assert templates == ["vmware://vm/{identifier}"]
        listing = json.loads((await client.read_resource("vmware://vms")).contents[0].text)
        assert listing["count"] == 3
        detail = json.loads(
            (await client.read_resource("vmware://vm/win11-golden")).contents[0].text
        )
        assert detail["name"] == "win11-golden"


async def test_a_bad_resource_identifier_reports_a_useful_error(server):
    from mcp.server.mcpserver.exceptions import ResourceError

    async with Client(server) as client:
        with pytest.raises(Exception) as excinfo:
            await client.read_resource("vmware://vm/nope")
    assert "No VM matches" in str(excinfo.value) or isinstance(excinfo.value, ResourceError)


async def test_prompts_are_registered(server):
    async with Client(server) as client:
        prompts = {prompt.name for prompt in (await client.list_prompts()).prompts}
    assert prompts == {"spin_up_test_vms", "run_windows_test", "reset_test_vms"}


async def test_spin_up_prompt_names_the_tools(server):
    async with Client(server) as client:
        rendered = await client.get_prompt(
            "spin_up_test_vms", {"template": "win11-golden", "count": "5"}
        )
    text = rendered.messages[0].content.text
    assert "win11-golden" in text
    assert "vmware_clone_many" in text
    assert "count=5" in text


async def test_reset_prompt_insists_on_a_dry_run(server):
    async with Client(server) as client:
        rendered = await client.get_prompt("reset_test_vms", {"name_prefix": "web-test"})
    text = rendered.messages[0].content.text
    assert "dry_run=true" in text
    assert "vmware_revert_many" in text


async def test_windows_test_prompt_warns_about_shell_semantics(server):
    async with Client(server) as client:
        rendered = await client.get_prompt(
            "run_windows_test", {"vm": "win11-golden", "installer_host_path": "/tmp/app.msi"}
        )
    text = rendered.messages[0].content.text
    assert "not run through a shell" in text


def test_create_server_builds_its_own_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VMWARE_VM_DIRS", str(tmp_path))
    monkeypatch.setenv("VMWARE_PERMISSION_MODE", "read-only")
    server = create_server()
    assert server.name == SERVER_NAME
