"""Bulk inventory retrieval via the vSphere PropertyCollector.

Walking managed objects attribute-by-attribute costs one round trip per
attribute, which is unusable against a real vCenter holding thousands of VMs.
Everything here batches into a single ``RetrievePropertiesEx`` call instead.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pyVmomi import vim, vmodl

from ..errors import VMwareMCPError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def require_manager(manager: T | None, name: str) -> T:
    """Assert that an optional ServiceContent manager is actually present.

    vCenter always publishes these, but ESXi omits a few (there is no
    ``perfManager`` on some builds) and the API types them as optional.
    """
    if manager is None:
        raise VMwareMCPError(
            f"The connected vSphere endpoint does not provide a {name}; "
            f"this operation is not available here."
        )
    return manager


@dataclass(frozen=True)
class ObjectRecord:
    """A managed object plus the subset of properties that were retrieved."""

    obj: Any
    moid: str
    type: str
    props: dict[str, Any] = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        value = self.props.get(path, default)
        return default if value is None else value


def moid_of(obj: Any) -> str | None:
    """The stable ``vim-type:moid`` value of a managed object reference."""
    if obj is None:
        return None
    return getattr(obj, "_moId", None)


def type_name(obj: Any) -> str | None:
    if obj is None:
        return None
    return type(obj).__name__


def collect_properties(
    service_instance: vim.ServiceInstance,
    obj_type: type,
    path_set: Sequence[str],
    *,
    container: Any = None,
    recursive: bool = True,
) -> list[ObjectRecord]:
    """Retrieve ``path_set`` for every ``obj_type`` beneath ``container``.

    Properties that are unset or that the caller may not read are simply absent
    from :attr:`ObjectRecord.props` rather than raising.
    """
    content = service_instance.RetrieveContent()
    root = container if container is not None else content.rootFolder
    view_manager = require_manager(content.viewManager, "view manager")
    view = view_manager.CreateContainerView(root, [obj_type], recursive)
    try:
        traversal = vmodl.query.PropertyCollector.TraversalSpec(
            name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView
        )
        obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
            obj=view, skip=True, selectSet=[traversal]
        )
        prop_spec = vmodl.query.PropertyCollector.PropertySpec(
            type=obj_type, all=False, pathSet=list(path_set)
        )
        filter_spec = vmodl.query.PropertyCollector.FilterSpec(
            objectSet=[obj_spec], propSet=[prop_spec]
        )
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        collector = content.propertyCollector

        records: list[ObjectRecord] = []
        result = collector.RetrievePropertiesEx(specSet=[filter_spec], options=options)
        while result is not None:
            records.extend(_records_from(result.objects))
            if not result.token:
                break
            result = collector.ContinueRetrievePropertiesEx(token=result.token)
        return records
    finally:
        _destroy_view(view)


def collect_properties_for(
    service_instance: vim.ServiceInstance,
    obj_type: type,
    objects: Sequence[Any],
    path_set: Sequence[str],
) -> list[ObjectRecord]:
    """Retrieve ``path_set`` for a known set of managed objects.

    Unlike :func:`collect_properties` this needs no container view, so it works
    for leaf objects such as a single VM.
    """
    if not objects:
        return []
    content = service_instance.RetrieveContent()
    obj_specs = [vmodl.query.PropertyCollector.ObjectSpec(obj=obj, skip=False) for obj in objects]
    prop_spec = vmodl.query.PropertyCollector.PropertySpec(
        type=obj_type, all=False, pathSet=list(path_set)
    )
    filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=obj_specs, propSet=[prop_spec])
    collector = content.propertyCollector
    result = collector.RetrievePropertiesEx(
        specSet=[filter_spec], options=vmodl.query.PropertyCollector.RetrieveOptions()
    )
    records: list[ObjectRecord] = []
    while result is not None:
        records.extend(_records_from(result.objects))
        if not result.token:
            break
        result = collector.ContinueRetrievePropertiesEx(token=result.token)
    return records


def _records_from(objects: Iterable[Any]) -> list[ObjectRecord]:
    records = []
    for obj_content in objects:
        props = {prop.name: prop.val for prop in (obj_content.propSet or [])}
        obj = obj_content.obj
        records.append(
            ObjectRecord(obj=obj, moid=moid_of(obj) or "", type=type_name(obj) or "", props=props)
        )
    return records


def _destroy_view(view: Any) -> None:
    try:
        view.DestroyView()
    except Exception:
        logger.debug("Failed to destroy container view", exc_info=True)


@dataclass(frozen=True)
class InventoryNode:
    """One entry in the inventory tree, as far as path resolution cares."""

    name: str
    type: str
    parent: str | None


class InventoryPathIndex:
    """Resolves managed objects to human readable inventory paths.

    vCenter models the inventory as a tree of folders, so a VM named ``web-01``
    might live at ``/Prod/vm/Tier1/web-01``. Clients (and models) need that path
    to tell same-named VMs apart across datacenters.
    """

    #: Managed object types fetched to build the tree. Leaf objects (VMs,
    #: datastores) are deliberately excluded: their parent moid is enough.
    CONTAINER_TYPES = (
        vim.Folder,
        vim.Datacenter,
        vim.ComputeResource,
        vim.ResourcePool,
        vim.HostSystem,
    )

    def __init__(self, nodes: dict[str, InventoryNode]) -> None:
        self._nodes = nodes

    @classmethod
    def build(cls, service_instance: vim.ServiceInstance) -> InventoryPathIndex:
        nodes: dict[str, InventoryNode] = {}
        for obj_type in cls.CONTAINER_TYPES:
            for record in collect_properties(service_instance, obj_type, ["name", "parent"]):
                nodes[record.moid] = InventoryNode(
                    name=record.props.get("name", ""),
                    type=record.type,
                    parent=moid_of(record.props.get("parent")),
                )
        return cls(nodes)

    @property
    def size(self) -> int:
        return len(self._nodes)

    def name_of(self, moid: str | None) -> str | None:
        if moid is None:
            return None
        node = self._nodes.get(moid)
        return node.name if node else None

    def _ancestry(self, moid: str | None) -> list[InventoryNode]:
        """Nodes from ``moid`` up to the root, inclusive."""
        chain: list[InventoryNode] = []
        seen: set[str] = set()
        current = moid
        while current and current not in seen:
            seen.add(current)
            node = self._nodes.get(current)
            if node is None:
                break
            chain.append(node)
            current = node.parent
        return chain

    def path_of(self, moid: str | None, leaf_name: str | None = None) -> str | None:
        """Render ``/Datacenter/vm/Folder/leaf``; ``None`` if nothing resolves."""
        chain = self._ancestry(moid)
        parts = [node.name for node in reversed(chain) if node.name]
        # vCenter's root folder is always reported as "Datacenters" and adds nothing.
        if parts and parts[0] == "Datacenters":
            parts = parts[1:]
        if leaf_name:
            parts.append(leaf_name)
        if not parts:
            return None
        return "/" + "/".join(parts)

    def datacenter_of(self, moid: str | None) -> str | None:
        """Name of the datacenter containing ``moid``, if it can be resolved."""
        for node in self._ancestry(moid):
            if node.type == "vim.Datacenter":
                return node.name
        return None
