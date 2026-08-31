"""Helpers shared by the tool modules."""

from __future__ import annotations

import fnmatch
import functools
import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import Settings
from ..errors import InvalidArgumentError, VMwareMCPError
from ..workstation import WorkstationClient

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool module needs: the Workstation client and its settings."""

    client: WorkstationClient
    settings: Settings


def mcp_tool(server: MCPServer, **kwargs: Any) -> Callable[[F], F]:
    """``server.tool`` with a cleaned docstring and anticipated-error wrapping.

    ``VMwareMCPError`` is raised as MCP ``ToolError`` so the client sees the
    real message (permission mode, unknown VM, …) instead of a generic crash.
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


def name_matches(value: str | None, pattern: str | None) -> bool:
    """Case-insensitive match; ``*``/``?`` switch from substring to glob matching."""
    if not pattern:
        return True
    if value is None:
        return False
    lowered_value = value.lower()
    lowered_pattern = pattern.lower()
    if any(char in lowered_pattern for char in "*?["):
        return fnmatch.fnmatch(lowered_value, lowered_pattern)
    return lowered_pattern in lowered_value


def equals_any(value: str | None, allowed: Sequence[str] | None) -> bool:
    if not allowed:
        return True
    if value is None:
        return False
    return value.lower() in {item.lower() for item in allowed}


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


def sort_by_name(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (item.get("name") or "").lower())


def require_non_empty(value: str, field: str) -> str:
    stripped = (value or "").strip()
    if not stripped:
        raise InvalidArgumentError(f"{field} must not be empty.")
    return stripped
