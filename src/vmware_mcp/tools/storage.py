"""Datastore and storage reporting."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ..vsphere import mappers
from ._common import ToolContext, mcp_tool, name_matches, paginate, sort_by_name

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_datastores(
        name: str | None = None,
        datacenter: str | None = None,
        datastore_type: str | None = None,
        min_used_percent: float | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List datastores with capacity, free space and over-provisioning.

        ``provisioned_gib`` includes space promised to thin disks that has not
        been written yet, so it can exceed ``capacity_gib``; ``overprovisioned``
        flags exactly that case.

        Args:
            name: Datastore name filter (substring, or glob with ``*``/``?``).
            datacenter: Only datastores in this datacenter.
            datastore_type: Filter by storage type such as ``VMFS``, ``NFS`` or
                ``vsan``.
            min_used_percent: Only datastores at or above this utilisation, for
                spotting the ones about to fill up.
            limit: Maximum number of datastores to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.Datastore, mappers.DATASTORE_PROPERTIES)
        datastores = [mappers.map_datastore(record, index) for record in records]
        datastores = [
            datastore
            for datastore in datastores
            if name_matches(datastore["name"], name)
            and (datacenter is None or datastore["datacenter"] == datacenter)
            and (
                datastore_type is None
                or (datastore["type"] or "").lower() == datastore_type.lower()
            )
            and (min_used_percent is None or (datastore["used_percent"] or 0) >= min_used_percent)
        ]
        page, meta = paginate(
            sort_by_name(datastores), limit=limit, offset=offset, settings=settings
        )
        total_capacity = sum(item["capacity_gib"] or 0 for item in datastores)
        total_free = sum(item["free_gib"] or 0 for item in datastores)
        return {
            **meta,
            "aggregate": {
                "capacity_gib": round(total_capacity, 2),
                "free_gib": round(total_free, 2),
                "used_percent": mappers.percent(total_capacity - total_free, total_capacity),
            },
            "datastores": page,
        }
