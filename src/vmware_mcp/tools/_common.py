"""Helpers shared by the tool modules."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..config import Settings
from ..errors import InvalidArgumentError, VMwareMCPError
from ..workstation import WorkstationClient

F = TypeVar("F", bound=Callable[..., Any])

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool module needs: the Workstation client and its settings."""

    client: WorkstationClient
    settings: Settings


def mcp_tool(server: MCPServer, **kwargs: Any) -> Callable[[F], F]:
    """``server.tool`` with a cleaned docstring and anticipated-error wrapping.

    ``VMwareMCPError`` is reported as an MCP ``ToolError`` so the client sees the
    real reason (permission mode, unknown VM, vmrun's own message) instead of a
    generic "error executing tool".
    """

    def decorator(func: F) -> F:
        description = inspect.cleandoc(func.__doc__) if func.__doc__ else None

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwds: Any) -> Any:
            try:
                return await func(*args, **kwds)
            except VMwareMCPError as exc:
                raise ToolError(str(exc)) from exc

        return server.tool(description=description, **kwargs)(wrapper)  # type: ignore[return-value]

    return decorator


def resolve_limit(limit: int | None, settings: Settings) -> int:
    if limit is None:
        return settings.default_page_size
    if limit < 1:
        raise InvalidArgumentError("limit must be at least 1.")
    return min(limit, settings.max_results)


def paginate(
    items: Sequence[Any], *, limit: int | None, offset: int, settings: Settings
) -> tuple[list[Any], dict[str, Any]]:
    if offset < 0:
        raise InvalidArgumentError("offset cannot be negative.")
    effective_limit = resolve_limit(limit, settings)
    page = list(items[offset : offset + effective_limit])
    total = len(items)
    return page, {
        "total_matched": total,
        "returned": len(page),
        "offset": offset,
        "limit": effective_limit,
        "truncated": offset + len(page) < total,
    }


__all__ = [
    "DESTRUCTIVE",
    "MUTATING",
    "READ_ONLY",
    "ToolContext",
    "mcp_tool",
    "paginate",
    "resolve_limit",
]
