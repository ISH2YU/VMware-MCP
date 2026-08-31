"""Helpers for driving the server the way a real MCP client would."""

from __future__ import annotations

from typing import Any

from mcp import Client
from mcp.types import CallToolResult


async def call_tool(server: Any, tool: str, /, **arguments: Any) -> CallToolResult:
    """Invoke a tool over an in-memory MCP session, errors included."""
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


async def call_ok(server: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool and assert it succeeded, returning the structured payload."""
    result = await call_tool(server, tool, **arguments)
    assert not result.is_error, error_text(result)
    assert result.structured_content is not None
    return result.structured_content


async def call_error(server: Any, tool: str, /, **arguments: Any) -> str:
    """Invoke a tool, assert it failed, and return the message the client sees."""
    result = await call_tool(server, tool, **arguments)
    assert result.is_error, f"expected {tool} to fail, got: {result.structured_content}"
    return error_text(result)


def error_text(result: CallToolResult) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)
