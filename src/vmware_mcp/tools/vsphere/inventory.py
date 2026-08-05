"""Inventory discovery tools: datacenters, clusters, hosts and resource pools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pyVmomi import vim

from ...vsphere import lookup, mappers
from ...vsphere.query import InventoryPathIndex, ObjectRecord, moid_of
from .._common import ToolContext, mcp_tool, name_matches, paginate, sort_by_name

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, context: ToolContext) -> None:
    client = context.client
    settings = context.settings

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_about() -> dict[str, Any]:
        """Identify the connected vSphere endpoint.

        Returns the product name and version of vCenter Server or ESXi, the API
        version, the account the server is authenticated as, and the permission
        mode this MCP server is running in. Call this first to find out whether
        write operations are allowed at all.
        """
        info = await client.about()
        return {
            "server": mappers.map_about_info(info["about"]),
            "session_user": info["session_user"],
            "server_time": mappers.as_timestamp(info["server_clock"]),
            "connection": settings.describe(),
        }

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_datacenters() -> dict[str, Any]:
        """List every datacenter in the vSphere inventory."""
        index = await client.path_index()
        records = await client.collect(vim.Datacenter, mappers.DATACENTER_PROPERTIES)
        datacenters = sort_by_name(mappers.map_datacenter(record, index) for record in records)
        return {"count": len(datacenters), "datacenters": datacenters}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_clusters(
        name: str | None = None,
        datacenter: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List compute clusters with their capacity, DRS and HA configuration.

        Args:
            name: Optional filter on cluster name. Plain text matches as a
                case-insensitive substring; ``*`` and ``?`` enable glob matching.
            datacenter: Only return clusters in this datacenter.
            limit: Maximum number of clusters to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.ClusterComputeResource, mappers.CLUSTER_PROPERTIES)
        clusters = [mappers.map_cluster(record, index) for record in records]
        clusters = [
            cluster
            for cluster in clusters
            if name_matches(cluster["name"], name)
            and (datacenter is None or cluster["datacenter"] == datacenter)
        ]
        page, meta = paginate(sort_by_name(clusters), limit=limit, offset=offset, settings=settings)
        return {**meta, "clusters": page}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_hosts(
        name: str | None = None,
        cluster: str | None = None,
        datacenter: str | None = None,
        connection_state: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List ESXi hosts with hardware, version and live CPU/memory utilisation.

        Args:
            name: Optional host name filter (substring, or glob with ``*``/``?``).
            cluster: Only return hosts belonging to this cluster.
            datacenter: Only return hosts in this datacenter.
            connection_state: Filter by ``connected``, ``disconnected`` or
                ``notResponding``.
            limit: Maximum number of hosts to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.HostSystem, mappers.HOST_PROPERTIES)
        hosts = [mappers.map_host(record, index) for record in records]
        hosts = [
            host
            for host in hosts
            if name_matches(host["name"], name)
            and (cluster is None or host["cluster"] == cluster)
            and (datacenter is None or host["datacenter"] == datacenter)
            and (
                connection_state is None
                or (host["connection_state"] or "").lower() == connection_state.lower()
            )
        ]
        page, meta = paginate(sort_by_name(hosts), limit=limit, offset=offset, settings=settings)
        return {**meta, "hosts": page}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_get_host(host: str) -> dict[str, Any]:
        """Full detail for one ESXi host, including attached VMs and datastores.

        Args:
            host: Host name, managed object id (``host-42``), hardware UUID or
                inventory path.
        """
        index = await client.path_index()
        record = await client.resolve(lookup.HOST, host, index=index)
        detailed = await client.properties_for(
            vim.HostSystem, record.moid, mappers.HOST_DETAIL_PROPERTIES
        )
        return {"host": mappers.map_host_detail(detailed, index)}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_list_resource_pools(
        name: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List resource pools with their reservations, limits and current usage.

        Args:
            name: Optional resource pool name filter.
            limit: Maximum number of pools to return.
            offset: Number of matches to skip, for paging.
        """
        index = await client.path_index()
        records = await client.collect(vim.ResourcePool, mappers.RESOURCE_POOL_PROPERTIES)
        pools = [
            mappers.map_resource_pool(record, index)
            for record in records
            if name_matches(record.get("name"), name)
        ]
        page, meta = paginate(sort_by_name(pools), limit=limit, offset=offset, settings=settings)
        return {**meta, "resource_pools": page}

    @mcp_tool(server, annotations=READ_ONLY)
    async def vsphere_search_inventory(
        query: str,
        types: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search the inventory by name across several object types at once.

        Useful when you know a name but not what kind of object it is, or when
        you need the managed object id to pass to another tool.

        Args:
            query: Name to search for (substring, or glob with ``*``/``?``).
            types: Object types to search. Defaults to all of ``vm``, ``host``,
                ``cluster``, ``datastore``, ``network``, ``datacenter``,
                ``resource_pool`` and ``folder``.
            limit: Maximum number of matches to return in total.
        """
        index = await client.path_index()
        selected = types or list(lookup.KINDS_BY_NAME)
        unknown = [item for item in selected if item not in lookup.KINDS_BY_NAME]
        if unknown:
            known = ", ".join(sorted(lookup.KINDS_BY_NAME))
            raise ValueError(f"Unknown object type(s): {', '.join(unknown)}. Known types: {known}.")

        matches: list[dict[str, Any]] = []
        for type_name in selected:
            kind = lookup.KINDS_BY_NAME[type_name]
            records = await client.collect(kind.vim_type, ("name", "parent"))
            for record in records:
                if not name_matches(record.get("name"), query):
                    continue
                matches.append(_search_hit(type_name, record, index))
        page, meta = paginate(sort_by_name(matches), limit=limit, offset=0, settings=settings)
        return {**meta, "matches": page}


def _search_hit(type_name: str, record: ObjectRecord, index: InventoryPathIndex) -> dict[str, Any]:
    parent_moid = moid_of(record.props.get("parent"))
    return {
        "type": type_name,
        "vim_type": record.type,
        "name": record.get("name"),
        "moid": record.moid,
        "path": index.path_of(parent_moid, record.get("name")),
        "datacenter": index.datacenter_of(parent_moid),
    }
