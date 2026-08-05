"""Virtual networking inventory."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...vsphere import mappers
from .._common import ToolContext, mcp_tool, name_matches, paginate, sort_by_name

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_networks(
        name: str | None = None,
        datacenter: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List networks: standard port groups, distributed port groups and opaque networks.

        Distributed port groups additionally report their VLAN configuration and
        the distributed switch they belong to.

        Args:
            name: Network name filter (substring, or glob with ``*``/``?``).
            datacenter: Only networks in this datacenter.
            kind: ``standard-portgroup``, ``distributed-portgroup`` or
                ``opaque-network``.
            limit: Maximum number of networks to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.Network, mappers.NETWORK_PROPERTIES)
        networks = {record.moid: mappers.map_network(record, index) for record in records}

        dvpg_records = await client.collect(
            vim.dvs.DistributedVirtualPortgroup, mappers.DVPORTGROUP_PROPERTIES
        )
        switch_names = await _switch_names(client)
        for record in dvpg_records:
            entry = networks.get(record.moid)
            if entry is None:
                continue
            extras = mappers.map_dvportgroup_extras(record)
            extras["dvs"] = switch_names.get(extras["dvs_moid"] or "")
            entry.update(extras)

        filtered = [
            network
            for network in networks.values()
            if name_matches(network["name"], name)
            and (datacenter is None or network["datacenter"] == datacenter)
            and (kind is None or network["kind"] == kind)
        ]
        page, meta = paginate(sort_by_name(filtered), limit=limit, offset=offset, settings=settings)
        return {**meta, "networks": page}


async def _switch_names(client: Any) -> dict[str, str]:
    records = await client.collect(vim.DistributedVirtualSwitch, ("name",))
    return {record.moid: record.get("name") for record in records}
