"""Command line entry point for the VMware MCP server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

import anyio

from .config import ENV_PREFIX, Settings, load_settings
from .errors import VMwareMCPError
from .vsphere import mappers
from .vsphere.client import VSphereClient

logger = logging.getLogger("vmware_mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmware-mcp",
        description=(
            "Model Context Protocol server for VMware vSphere. Configuration comes from "
            f"{ENV_PREFIX}* environment variables; the flags below override them."
        ),
    )
    connection = parser.add_argument_group("vSphere connection")
    connection.add_argument("--vsphere-host", metavar="HOST", help="vCenter or ESXi hostname.")
    connection.add_argument("--vsphere-port", type=int, metavar="PORT", help="Default 443.")
    connection.add_argument("--username", metavar="USER", help="vSphere username.")
    connection.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification. Lab use only.",
    )
    connection.add_argument("--ca-bundle", metavar="PATH", help="CA certificate bundle to trust.")
    connection.add_argument(
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
        help="Connect to vSphere, print what was found and exit. Use this to verify credentials.",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Environment settings with the CLI overrides applied on top."""
    overrides = {
        f"{ENV_PREFIX}HOST": args.vsphere_host,
        f"{ENV_PREFIX}PORT": str(args.vsphere_port) if args.vsphere_port else None,
        f"{ENV_PREFIX}USERNAME": args.username,
        f"{ENV_PREFIX}CA_BUNDLE": args.ca_bundle,
        f"{ENV_PREFIX}PERMISSION_MODE": args.permission_mode,
        f"{ENV_PREFIX}LOG_LEVEL": args.log_level,
        f"{ENV_PREFIX}VERIFY_SSL": "false" if args.insecure else None,
    }
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
    """Verify credentials and print a short inventory summary."""
    client = VSphereClient(settings)
    try:
        info = await client.about()
        index = await client.path_index()
        report = {
            "endpoint": settings.endpoint,
            "permission_mode": settings.permission_mode.value,
            "verify_ssl": settings.verify_ssl,
            "authenticated_as": info["session_user"],
            "server": mappers.map_about_info(info["about"]),
            "inventory_objects_indexed": index.size,
        }
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

    from .server import create_server  # imported late so --check stays fast

    server = create_server(settings)
    logger.info(
        "Starting vmware-mcp against %s in %s mode over %s",
        settings.endpoint,
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
