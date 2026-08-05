"""Command line entry point for the VMware MCP server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

import anyio

from .config import (
    ENV_PREFIX,
    BaseSettings,
    VSphereSettings,
    WorkstationSettings,
    detect_backend,
    load_settings,
)
from .errors import VMwareMCPError

logger = logging.getLogger("vmware_mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmware-mcp",
        description=(
            "Model Context Protocol server for VMware. Defaults to local Workstation / "
            f"Fusion / Player. Point {ENV_PREFIX}HOST at a vCenter to use the vSphere "
            "backend instead. Configuration comes from environment variables; flags override."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["workstation", "vsphere"],
        help="Force a backend. Default: workstation, unless VMWARE_HOST is set.",
    )

    local = parser.add_argument_group("local Workstation / Fusion")
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

    remote = parser.add_argument_group("vSphere (vCenter / ESXi)")
    remote.add_argument("--vsphere-host", metavar="HOST", help="vCenter or ESXi hostname.")
    remote.add_argument("--vsphere-port", type=int, metavar="PORT", help="Default 443.")
    remote.add_argument("--username", metavar="USER", help="vSphere username.")
    remote.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification. Lab use only.",
    )
    remote.add_argument("--ca-bundle", metavar="PATH", help="CA certificate bundle to trust.")

    shared = parser.add_argument_group("shared")
    shared.add_argument(
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
        help="Verify the backend is reachable, print a short summary and exit.",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> BaseSettings:
    """Environment settings with the CLI overrides applied on top."""
    overrides: dict[str, str | None] = {
        f"{ENV_PREFIX}BACKEND": args.backend,
        f"{ENV_PREFIX}VMRUN_PATH": args.vmrun,
        f"{ENV_PREFIX}PRODUCT": args.product,
        f"{ENV_PREFIX}GUEST_USERNAME": args.guest_user,
        f"{ENV_PREFIX}HOST": args.vsphere_host,
        f"{ENV_PREFIX}PORT": str(args.vsphere_port) if args.vsphere_port else None,
        f"{ENV_PREFIX}USERNAME": args.username,
        f"{ENV_PREFIX}CA_BUNDLE": args.ca_bundle,
        f"{ENV_PREFIX}PERMISSION_MODE": args.permission_mode,
        f"{ENV_PREFIX}LOG_LEVEL": args.log_level,
        f"{ENV_PREFIX}VERIFY_SSL": "false" if args.insecure else None,
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


async def check_connection(settings: BaseSettings) -> int:
    """Verify the backend and print a short summary."""
    if isinstance(settings, WorkstationSettings):
        from .workstation import WorkstationClient

        client = WorkstationClient(settings)
        try:
            report = await client.about()
            print(json.dumps(report, indent=2))
            return 0
        finally:
            await client.close()

    assert isinstance(settings, VSphereSettings)
    from .vsphere import mappers
    from .vsphere.client import VSphereClient

    vsphere = VSphereClient(settings)
    try:
        info = await vsphere.about()
        index = await vsphere.path_index()
        report = {
            "backend": "vsphere",
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
        await vsphere.close()


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
    target = (
        f"local {settings.product.value}"
        if isinstance(settings, WorkstationSettings)
        else settings.endpoint  # type: ignore[attr-defined]
    )
    logger.info(
        "Starting vmware-mcp (%s) against %s in %s mode over %s",
        detect_backend(os.environ).value if args.backend is None else args.backend,
        target,
        settings.permission_mode.value,
        args.transport,
    )
    # Prefer the settings' own backend tag for the log line.
    logger.info("Backend: %s", settings.backend.value)

    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
