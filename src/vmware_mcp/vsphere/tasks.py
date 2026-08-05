"""Waiting on vSphere tasks from async code.

Long operations (clone, vMotion, snapshot) return a ``vim.Task`` immediately.
Rather than blocking a worker thread for the duration, the task is polled from
the event loop so progress can be streamed back to the MCP client and the wait
can be cancelled without stranding a thread.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import anyio
from pyVmomi import vim

from ..errors import TaskFailedError, TaskTimeoutError, VMwareMCPError
from .mappers import map_task_info
from .query import moid_of

logger = logging.getLogger(__name__)

_POLL_INITIAL_SECONDS = 0.5
_POLL_MAX_SECONDS = 5.0
_TERMINAL_STATES = {"success", "error"}


class ProgressReporter(Protocol):
    """The subset of the MCP request context this module needs."""

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None: ...


def task_id(task: Any) -> str:
    moid = moid_of(task)
    if not moid:
        raise VMwareMCPError(
            "vSphere accepted the request but returned no task reference, so its progress "
            "cannot be tracked. Check the recent tasks in vCenter before retrying."
        )
    return moid


def read_task_info(service_instance: vim.ServiceInstance, moid: str) -> Any:
    """Fetch ``TaskInfo`` for a task moid on the current session."""
    return vim.Task(moid, service_instance._stub).info


async def wait_for_task(
    client: Any,
    task: Any,
    *,
    operation: str,
    timeout: int | None = None,
    reporter: ProgressReporter | None = None,
) -> dict[str, Any]:
    """Poll ``task`` until it finishes.

    Returns the mapped task info on success. Raises :class:`TaskFailedError` if
    vSphere reports an error and :class:`TaskTimeoutError` if the deadline
    passes -- in which case the task itself is still running server-side.
    """
    moid = task_id(task)
    limit = client.settings.task_timeout if timeout is None else timeout
    delay = _POLL_INITIAL_SECONDS
    last_progress = -1.0

    with anyio.move_on_after(limit) as scope:
        while True:
            info = await client.call(read_task_info, moid)
            state = str(getattr(info, "state", ""))
            if reporter is not None:
                progress = float(getattr(info, "progress", None) or 0)
                if progress > last_progress:
                    last_progress = progress
                    await reporter.report_progress(progress, 100.0, f"{operation}: {state}")
            if state in _TERMINAL_STATES:
                mapped = map_task_info(info)
                if state == "error":
                    raise TaskFailedError(
                        f"{operation} failed: {mapped['error'] or 'vSphere reported an error'}"
                    )
                if reporter is not None:
                    await reporter.report_progress(100.0, 100.0, f"{operation}: completed")
                return mapped
            await anyio.sleep(delay)
            delay = min(delay * 1.5, _POLL_MAX_SECONDS)

    if scope.cancelled_caught:
        raise TaskTimeoutError(
            f"{operation} did not finish within {limit}s. The task is still running in "
            f"vSphere; poll it with vsphere_get_task using task_id {moid!r}.",
            task_id=moid,
        )
    raise AssertionError("unreachable")  # pragma: no cover


async def run_task(
    client: Any,
    start: Any,
    *,
    operation: str,
    wait: bool = True,
    timeout: int | None = None,
    reporter: ProgressReporter | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a vSphere task and optionally wait for it.

    ``start`` is a callable taking the service instance and returning a
    ``vim.Task``. When ``wait`` is false the task id is returned straight away
    so the caller can poll with ``vsphere_get_task``.
    """
    task = await client.call(start)
    moid = task_id(task)
    payload: dict[str, Any] = {"operation": operation, "task_id": moid, **(result or {})}
    if not wait:
        return {**payload, "status": "running", "waited": False}
    info = await wait_for_task(
        client, task, operation=operation, timeout=timeout, reporter=reporter
    )
    return {**payload, "status": "completed", "waited": True, "task": info}
