"""Server metadata, resources and prompts as an MCP client sees them."""

from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import call_ok
from vmware_mcp.config import PermissionMode
from vmware_mcp.server import build_instructions
from vmware_mcp.tools.vsphere import MODULES

EXPECTED_TOOL_COUNT = 27


async def test_every_tool_is_advertised_with_a_description_and_annotations(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) == EXPECTED_TOOL_COUNT
    for tool in tools:
        assert tool.name.startswith("vsphere_"), tool.name
        assert tool.description, tool.name
        # The docstring must be dedented, not indented by the nesting level.
        assert "\n    " not in tool.description.split("Args:")[0], tool.name
        assert tool.annotations is not None, tool.name
        assert tool.input_schema["type"] == "object"


async def test_mutating_tools_are_not_marked_read_only(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in (
        "vsphere_change_vm_power_state",
        "vsphere_create_snapshot",
        "vsphere_delete_vm",
        "vsphere_clone_vm",
    ):
        assert tools[name].annotations.read_only_hint is False, name
    for name in ("vsphere_list_vms", "vsphere_get_vm", "vsphere_list_alarms"):
        assert tools[name].annotations.read_only_hint is True, name


async def test_deleting_tools_carry_the_destructive_hint(server):
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert tools["vsphere_delete_vm"].annotations.destructive_hint is True
    assert tools["vsphere_revert_to_snapshot"].annotations.destructive_hint is True
    assert tools["vsphere_create_snapshot"].annotations.destructive_hint is False


async def test_tool_names_are_unique_across_modules():
    names = [module.__name__ for module in MODULES]
    assert len(names) == len(set(names))


async def test_server_advertises_itself(server):
    async with Client(server) as client:
        info = client.server_info
        instructions = client.instructions
    assert info.name == "vmware-mcp"
    assert "vSphere" in (instructions or "")


async def test_inventory_summary_resource(server):
    async with Client(server) as client:
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == ["vsphere://inventory/summary"]
        contents = await client.read_resource("vsphere://inventory/summary")
    summary = json.loads(contents.contents[0].text)
    assert summary["datacenters"] == 1
    assert summary["clusters"] == 1
    assert summary["hosts"] == {
        "total": 2,
        "connected": 2,
        "in_maintenance": 0,
        "cpu_cores": 48,
        "memory_gib": 512.0,
    }
    assert summary["virtual_machines"]["total"] == 2
    assert summary["virtual_machines"]["templates"] == 1
    assert summary["storage"]["datastores"] == 2


async def test_vm_detail_resource_template(server):
    async with Client(server) as client:
        templates = (await client.list_resource_templates()).resource_templates
        assert [template.uri_template for template in templates] == ["vsphere://vm/{identifier}"]
        contents = await client.read_resource("vsphere://vm/web-01")
    vm = json.loads(contents.contents[0].text)
    assert vm["moid"] == "vm-101"
    assert vm["snapshots"]["count"] == 2


async def test_prompts_are_available_and_reference_real_tools(server):
    async with Client(server) as client:
        prompts = {prompt.name for prompt in (await client.list_prompts()).prompts}
        assert prompts == {"troubleshoot_vm", "capacity_report"}
        rendered = await client.get_prompt("troubleshoot_vm", {"vm": "web-01"})
    text = rendered.messages[0].content.text
    assert "web-01" in text
    for tool in ("vsphere_get_vm", "vsphere_get_performance", "vsphere_list_alarms"):
        assert tool in text


async def test_capacity_report_prompt_takes_an_optional_scope(server):
    async with Client(server) as client:
        default = await client.get_prompt("capacity_report", {})
        scoped = await client.get_prompt("capacity_report", {"scope": "Cluster-A"})
    assert "the whole environment" in default.messages[0].content.text
    assert "Cluster-A" in scoped.messages[0].content.text


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (PermissionMode.READ_ONLY, "READ-ONLY"),
        (PermissionMode.WRITE, "may change VMs"),
        (PermissionMode.DESTRUCTIVE, "FULL access"),
    ],
)
def test_instructions_state_what_the_server_may_do(mode, expected):
    from conftest import make_settings

    instructions = build_instructions(make_settings(permission_mode=mode))
    assert expected in instructions
    assert "vcenter.lab.local:443" in instructions


async def test_tools_are_registered_once_per_server(server_factory):
    first = server_factory()
    second = server_factory()
    async with Client(first) as client:
        assert len((await client.list_tools()).tools) == EXPECTED_TOOL_COUNT
    async with Client(second) as client:
        assert len((await client.list_tools()).tools) == EXPECTED_TOOL_COUNT


async def test_a_read_tool_still_works_after_the_client_reconnects(server):
    first = await call_ok(server, "vsphere_list_vms")
    second = await call_ok(server, "vsphere_list_vms")
    assert first["total_matched"] == second["total_matched"] == 2
