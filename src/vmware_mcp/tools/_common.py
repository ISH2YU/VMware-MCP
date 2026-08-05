"""Helpers shared by the tool modules."""

from __future__ import annotations

import fnmatch
import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server import MCPServer

from ..config import BaseSettings
from ..errors import InvalidArgumentError

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool module needs: a backend client and its settings."""

    client: Any
    settings: BaseSettings


def mcp_tool(server: MCPServer, **kwargs: Any) -> Callable[[F], F]:
    """``server.tool`` with the docstring dedented before it reaches the client.

    The SDK passes ``__doc__`` through verbatim, which leaves eight spaces of
    indentation on every line of a nested tool function's description.
    """

    def decorator(func: F) -> F:
        description = inspect.cleandoc(func.__doc__) if func.__doc__ else None
        return server.tool(description=description, **kwargs)(func)

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
    """True when ``allowed`` is empty or contains ``value`` (case-insensitively)."""
    if not allowed:
        return True
    if value is None:
        return False
    return value.lower() in {item.lower() for item in allowed}


def resolve_limit(limit: int | None, settings: BaseSettings) -> int:
    if limit is None:
        return settings.default_page_size
    if limit < 1:
        raise InvalidArgumentError("limit must be at least 1.")
    return min(limit, settings.max_results)


def paginate(
    items: Sequence[Any], *, limit: int | None, offset: int, settings: BaseSettings
) -> tuple[list[Any], dict[str, Any]]:
    """Slice ``items`` and describe the slice.

    Listings are capped so a folder with hundreds of VMs cannot blow up a
    model's context window; ``truncated`` tells the caller to page rather than
    assume it has seen everything.
    """
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
