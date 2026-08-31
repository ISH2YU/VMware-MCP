"""Command line entry point for the VMware MCP server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

import anyio

from . import __version__
from .config import ENV_PREFIX, Settings, load_settings
from .errors import VMwareMCPError

logger = logging.getLogger("vmware_mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmware-mcp",
        description=(
            "Model Context Protocol server for local VMware Workstation, Fusion or Player. "
            f"Configuration comes from {ENV_PREFIX}* environment variables; flags override them."
        ),
    )
    local = parser.add_argument_group("VMware Workstation / Fusion")
    local.add_argument("--vmrun", metavar="PATH", help="Path to the vmrun executable.")
    local.add_argument(
        "--product",
        choices=["ws", "fusion", "player"],
        help="vmrun host type. Default: ws on Windows/Linux, fusion on macOS.",
    )
    local.add_argument(
        "--vm-dir",
        action="append",
        dest="vm_dirs",
        metavar="DIR",
        help="Directory to scan for .vmx files. Repeatable. Overrides VMWARE_VM_DIRS.",
    )
    local.add_argument("--guest-user", metavar="USER", help="Default guest OS username.")
    local.add_argument(
        "--permission-mode",
        choices=["read-only", "write", "destructive"],
        help="How much the server is allowed to change. Default read-only.",
    )

    transport = parser.add_argument_group("MCP transport")
    transport.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.environ.get(f"{ENV_PREFIX}TRANSPORT", "stdio"),
        help="Default stdio, which is what desktop MCP clients expect.",
    )
    transport.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transports.")
    transport.add_argument("--port", type=int, default=8000, help="Port for HTTP transports.")

    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Find vmrun, scan VM directories, print a short summary and exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"vmware-mcp {__version__}",
        help="Print the version and exit.",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Environment settings with the CLI overrides applied on top."""
    overrides: dict[str, str | None] = {
        f"{ENV_PREFIX}VMRUN_PATH": args.vmrun,
        f"{ENV_PREFIX}PRODUCT": args.product,
        f"{ENV_PREFIX}GUEST_USERNAME": args.guest_user,
        f"{ENV_PREFIX}PERMISSION_MODE": args.permission_mode,
        f"{ENV_PREFIX}LOG_LEVEL": args.log_level,
    }
    if args.vm_dirs:
        overrides[f"{ENV_PREFIX}VM_DIRS"] = os.pathsep.join(args.vm_dirs)

    env = dict(os.environ)
    env.update({key: value for key, value in overrides.items() if value is not None})
    return load_settings(env)


def configure_logging(level: str) -> None:
    # stdout carries the MCP protocol on the stdio transport, so logs go to stderr.
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


async def check_connection(settings: Settings) -> int:
    """Verify vmrun and print a short inventory summary."""
    from .workstation import WorkstationClient

    client = WorkstationClient(settings)
    try:
        report = await client.about()
        print(json.dumps(report, indent=2))
        return 0
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = settings_from_args(args)
    except VMwareMCPError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or settings.log_level)

    if args.check:
        try:
            return anyio.run(check_connection, settings)
        except VMwareMCPError as exc:
            print(f"Connection check failed: {exc}", file=sys.stderr)
            return 1

    from .server import create_server

    server = create_server(settings)
    logger.info(
        "Starting vmware-mcp for local %s in %s mode over %s",
        settings.product.value,
        settings.permission_mode.value,
        args.transport,
    )
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
