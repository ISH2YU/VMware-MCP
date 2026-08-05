from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

sys.path.insert(0, str(Path(__file__).parent))

from fake_vsphere import FakeInventory, FakeSession, build_inventory
from vmware_mcp.config import PermissionMode, Settings
from vmware_mcp.server import create_server
from vmware_mcp.tools import ToolContext
from vmware_mcp.vsphere.client import VSphereClient


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "host": "vcenter.lab.local",
        "username": "svc-mcp@vsphere.local",
        "password": "secret",
        "permission_mode": PermissionMode.READ_ONLY,
        "cache_ttl": 0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def inventory() -> FakeInventory:
    return build_inventory()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def fake_session(inventory: FakeInventory) -> FakeSession:
    return FakeSession(inventory)


@pytest.fixture
def client(fake_session: FakeSession, settings: Settings) -> VSphereClient:
    return VSphereClient(settings, session=fake_session)


@pytest.fixture
def context(client: VSphereClient, settings: Settings) -> ToolContext:
    return ToolContext(client=client, settings=settings)


@pytest.fixture
def server_factory(fake_session: FakeSession):
    """Build a server wired to the fake inventory at a chosen permission mode."""

    def factory(permission_mode: PermissionMode = PermissionMode.READ_ONLY):
        settings = make_settings(permission_mode=permission_mode)
        vsphere = VSphereClient(settings, session=fake_session)
        return create_server(settings, client=vsphere)

    return factory


@pytest.fixture
def server(server_factory):
    return server_factory()


async def call_tool(server: Any, tool: str, /, **arguments: Any) -> CallToolResult:
    """Invoke a tool over an in-memory MCP session, as a real client would.

    ``server`` and ``tool`` are positional-only so that tool arguments named
    ``name`` or ``server`` can be passed as keywords.
    """
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


async def call_ok(server: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool and return its structured result, failing on tool errors."""
    result = await call_tool(server, tool, **arguments)
    assert not result.is_error, error_text(result)
    assert result.structured_content is not None
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)
