"""Server metadata, resources and prompts as an MCP client sees them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from fake_vmrun import FakeVmrun, write_vmx
from vmware_mcp.config import PermissionMode, Product, Settings
from vmware_mcp.server import build_instructions, create_server
from vmware_mcp.tools import MODULES
from vmware_mcp.workstation import WorkstationClient

EXPECTED_TOOL_COUNT = 20


@pytest.fixture
def server(tmp_path: Path):
    write_vmx(tmp_path, "win11-golden", guest_os="windows11-64")
    settings = Settings(
        vm_dirs=(tmp_path,),
        product=Product.WORKSTATION,
        permission_mode=PermissionMode.WRITE,
        guest_username="Administrator",
        guest_password="x",
        cache_ttl=0,
    )
    fake = FakeVmrun(executable_path=tmp_path / "vmrun")
    return create_server(settings, client=WorkstationClient(settings, runner=fake))  # type: ignore[arg-type]


async def test_every_tool_is_advertised(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) == EXPECTED_TOOL_COUNT
    for tool in tools:
        assert tool.name.startswith("vmware_"), tool.name
        assert tool.description, tool.name
        assert tool.annotations is not None, tool.name


async def test_mutating_tools_are_marked(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert tools["vmware_clone_many"].annotations.read_only_hint is False
    assert tools["vmware_delete_vm"].annotations.destructive_hint is True
    assert tools["vmware_list_vms"].annotations.read_only_hint is True


async def test_tool_modules_are_unique():
    assert len(MODULES) == len({module.__name__ for module in MODULES})


async def test_server_advertises_itself(server):
    async with Client(server) as client:
        assert client.server_info.name == "vmware-mcp"
        assert "Workstation" in (client.server_info.title or "")
        assert "Windows test-lab" in (client.instructions or "") or "clone_many" in (
            client.instructions or ""
        )


async def test_vm_resources(server):
    async with Client(server) as client:
        resources = [str(r.uri) for r in (await client.list_resources()).resources]
        assert resources == ["vmware://vms"]
        templates = [
            t.uri_template for t in (await client.list_resource_templates()).resource_templates
        ]
        assert templates == ["vmware://vm/{identifier}"]
        listing = json.loads((await client.read_resource("vmware://vms")).contents[0].text)
        assert listing["count"] == 1
        detail = json.loads(
            (await client.read_resource("vmware://vm/win11-golden")).contents[0].text
        )
        assert detail["name"] == "win11-golden"


async def test_prompts(server):
    async with Client(server) as client:
        prompts = {prompt.name for prompt in (await client.list_prompts()).prompts}
        assert prompts == {"spin_up_test_vms", "run_windows_test", "reset_test_vms"}
        rendered = await client.get_prompt(
            "spin_up_test_vms", {"template": "win11-golden", "count": "5"}
        )
    text = rendered.messages[0].content.text
    assert "win11-golden" in text
    assert "vmware_clone_many" in text


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
