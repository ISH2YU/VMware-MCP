from __future__ import annotations

from typing import Any

from mcp import Client
from mcp.types import CallToolResult


async def call_tool(server: Any, tool: str, /, **arguments: Any) -> CallToolResult:
    """Invoke a tool over an in-memory MCP session."""
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


async def call_ok(server: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await call_tool(server, tool, **arguments)
    assert not result.is_error, error_text(result)
    assert result.structured_content is not None
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)
