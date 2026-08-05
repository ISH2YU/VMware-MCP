"""Exception types raised by the VMware MCP server.

Anything deriving from :class:`VMwareMCPError` carries a message that is safe and
useful to hand straight back to an MCP client; the MCP SDK turns a raised
exception into a tool error result.
"""

from __future__ import annotations


class VMwareMCPError(Exception):
    """Base class for all errors surfaced by this server."""


class ConfigurationError(VMwareMCPError):
    """The server is missing configuration or was configured with invalid values."""


class ConnectionFailedError(VMwareMCPError):
    """Could not establish or re-establish a session with vCenter/ESXi."""


class PermissionDeniedError(VMwareMCPError):
    """A tool was called that the configured permission mode does not allow."""


class ObjectNotFoundError(VMwareMCPError):
    """No inventory object matched the supplied identifier."""


class AmbiguousObjectError(VMwareMCPError):
    """More than one inventory object matched the supplied identifier."""


class InvalidArgumentError(VMwareMCPError):
    """A tool argument failed validation before any vSphere call was made."""


class TaskFailedError(VMwareMCPError):
    """A vSphere task finished in the ``error`` state."""


class TaskTimeoutError(VMwareMCPError):
    """A vSphere task did not finish within the configured timeout.

    The task itself keeps running on the server; ``task_id`` can be polled with
    the ``vsphere_get_task`` tool.
    """

    def __init__(self, message: str, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id
