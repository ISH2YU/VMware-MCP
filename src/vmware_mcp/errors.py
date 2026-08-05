"""Exception types raised by the VMware MCP server.

Anything deriving from :class:`VMwareMCPError` carries a message that is safe and
useful to hand straight back to an MCP client.
"""

from __future__ import annotations


class VMwareMCPError(Exception):
    """Base class for all errors surfaced by this server."""


class ConfigurationError(VMwareMCPError):
    """The server is missing configuration or was configured with invalid values."""


class PermissionDeniedError(VMwareMCPError):
    """A tool was called that the configured permission mode does not allow."""


class ObjectNotFoundError(VMwareMCPError):
    """No virtual machine matched the supplied identifier."""


class AmbiguousObjectError(VMwareMCPError):
    """More than one virtual machine matched the supplied identifier."""


class InvalidArgumentError(VMwareMCPError):
    """A tool argument failed validation before any VMware call was made."""


class VmrunNotFoundError(VMwareMCPError):
    """The ``vmrun`` command line tool could not be located."""


class VmrunError(VMwareMCPError):
    """``vmrun`` ran but reported a failure."""

    def __init__(self, message: str, *, command: str = "", exit_code: int | None = None) -> None:
        super().__init__(message)
        self.command = command
        self.exit_code = exit_code


class CommandTimeoutError(VMwareMCPError):
    """A ``vmrun`` invocation did not finish in time and was killed."""


class GuestOperationError(VMwareMCPError):
    """An operation inside the guest OS failed, typically a VMware Tools problem."""
