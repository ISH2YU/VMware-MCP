"""Resolving user-supplied identifiers to managed objects.

An MCP client will refer to a VM as ``"web-01"``, ``"vm-4218"``, a BIOS UUID or
``"/Prod/vm/Tier1/web-01"`` depending on what it happens to have seen. All four
are accepted, and ambiguity is reported rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pyVmomi import vim

from ..errors import AmbiguousObjectError, ObjectNotFoundError
from .query import InventoryPathIndex, ObjectRecord, collect_properties, moid_of


@dataclass(frozen=True)
class EntityKind:
    """Describes one searchable inventory type."""

    label: str
    vim_type: type
    id_properties: tuple[str, ...] = ()

    @property
    def lookup_properties(self) -> tuple[str, ...]:
        return ("name", "parent", *self.id_properties)


VM = EntityKind("virtual machine", vim.VirtualMachine, ("config.uuid", "config.instanceUuid"))
HOST = EntityKind("host", vim.HostSystem, ("hardware.systemInfo.uuid",))
CLUSTER = EntityKind("cluster", vim.ClusterComputeResource)
COMPUTE_RESOURCE = EntityKind("compute resource", vim.ComputeResource)
DATASTORE = EntityKind("datastore", vim.Datastore)
NETWORK = EntityKind("network", vim.Network)
DATACENTER = EntityKind("datacenter", vim.Datacenter)
RESOURCE_POOL = EntityKind("resource pool", vim.ResourcePool)
FOLDER = EntityKind("folder", vim.Folder)

KINDS_BY_NAME: dict[str, EntityKind] = {
    "vm": VM,
    "host": HOST,
    "cluster": CLUSTER,
    "datastore": DATASTORE,
    "network": NETWORK,
    "datacenter": DATACENTER,
    "resource_pool": RESOURCE_POOL,
    "folder": FOLDER,
}


def match_records(
    records: Sequence[ObjectRecord],
    identifier: str,
    kind: EntityKind,
    index: InventoryPathIndex | None = None,
) -> list[ObjectRecord]:
    """Candidates for ``identifier``, most specific match tier first.

    Tiers are tried in order and the first non-empty one wins, so an exact moid
    always beats a case-insensitive name collision.
    """
    needle = identifier.strip()
    if not needle:
        return []
    lowered = needle.lower()

    by_moid = [record for record in records if record.moid == needle]
    if by_moid:
        return by_moid

    by_id_property = [
        record
        for record in records
        if any(
            isinstance(record.props.get(prop), str) and record.props[prop].lower() == lowered
            for prop in kind.id_properties
        )
    ]
    if by_id_property:
        return by_id_property

    by_name = [record for record in records if record.get("name") == needle]
    if by_name:
        return by_name

    if "/" in needle and index is not None:
        wanted = "/" + needle.strip("/").lower()
        by_path = [
            record
            for record in records
            if (index.path_of(moid_of(record.props.get("parent")), record.get("name")) or "")
            .lower()
            .endswith(wanted)
        ]
        if by_path:
            return by_path

    return [record for record in records if (record.get("name") or "").lower() == lowered]


def describe_candidates(
    records: Sequence[ObjectRecord], index: InventoryPathIndex | None = None
) -> list[str]:
    described = []
    for record in records:
        path = (
            index.path_of(moid_of(record.props.get("parent")), record.get("name"))
            if index
            else None
        )
        described.append(f"{record.get('name')} ({record.moid}{', ' + path if path else ''})")
    return described


def resolve_entity(
    service_instance: vim.ServiceInstance,
    kind: EntityKind,
    identifier: str,
    index: InventoryPathIndex | None = None,
    *,
    extra_properties: Sequence[str] = (),
) -> ObjectRecord:
    """Find exactly one object of ``kind`` matching ``identifier``.

    Raises :class:`ObjectNotFoundError` or :class:`AmbiguousObjectError`.
    """
    properties = tuple(dict.fromkeys((*kind.lookup_properties, *extra_properties)))
    records = collect_properties(service_instance, kind.vim_type, properties)
    matches = match_records(records, identifier, kind, index)
    if not matches:
        raise ObjectNotFoundError(
            f"No {kind.label} matches {identifier!r}. Accepted identifiers are the object "
            f"name, its managed object id, its UUID or its inventory path."
        )
    if len(matches) > 1:
        options = ", ".join(describe_candidates(matches, index))
        raise AmbiguousObjectError(
            f"{len(matches)} objects match {identifier!r}: {options}. "
            f"Use the managed object id to disambiguate."
        )
    return matches[0]


def managed_object(service_instance: vim.ServiceInstance, vim_type: type, moid: str) -> Any:
    """Rebuild a managed object reference from a moid on the current session.

    Stubs captured before a reconnect are stale, so long-running flows re-derive
    the reference from the moid instead of holding on to the object.
    """
    return vim_type(moid, service_instance._stub)
