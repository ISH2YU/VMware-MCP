"""Identifier resolution: which object did the caller mean?"""

from __future__ import annotations

import pytest
from pyVmomi import vim

from vmware_mcp.errors import AmbiguousObjectError, ObjectNotFoundError
from vmware_mcp.vsphere import lookup
from vmware_mcp.vsphere.query import InventoryNode, InventoryPathIndex, ObjectRecord

INDEX = InventoryPathIndex(
    {
        "group-d1": InventoryNode("Datacenters", "vim.Folder", None),
        "datacenter-2": InventoryNode("DC1", "vim.Datacenter", "group-d1"),
        "datacenter-5": InventoryNode("DC2", "vim.Datacenter", "group-d1"),
        "group-v3": InventoryNode("vm", "vim.Folder", "datacenter-2"),
        "group-v6": InventoryNode("vm", "vim.Folder", "datacenter-5"),
    }
)


def vm_record(moid: str, name: str, parent: str = "group-v3", **props) -> ObjectRecord:
    return ObjectRecord(
        obj=vim.VirtualMachine(moid, None),
        moid=moid,
        type="vim.VirtualMachine",
        props={"name": name, "parent": vim.Folder(parent, None), **props},
    )


RECORDS = [
    vm_record("vm-101", "web-01", **{"config.uuid": "421a-bios", "config.instanceUuid": "501a"}),
    vm_record("vm-102", "WEB-01", parent="group-v6"),
    vm_record("vm-103", "db-01"),
]


def test_moid_beats_a_name_collision():
    assert [r.moid for r in lookup.match_records(RECORDS, "vm-102", lookup.VM, INDEX)] == ["vm-102"]


def test_uuid_lookup_is_case_insensitive():
    matched = lookup.match_records(RECORDS, "421A-BIOS", lookup.VM, INDEX)
    assert [record.moid for record in matched] == ["vm-101"]


def test_instance_uuid_also_resolves():
    assert [r.moid for r in lookup.match_records(RECORDS, "501a", lookup.VM, INDEX)] == ["vm-101"]


def test_exact_name_beats_a_case_insensitive_match():
    assert [r.moid for r in lookup.match_records(RECORDS, "web-01", lookup.VM, INDEX)] == ["vm-101"]
    assert [r.moid for r in lookup.match_records(RECORDS, "WEB-01", lookup.VM, INDEX)] == ["vm-102"]


def test_a_path_disambiguates_same_named_vms():
    matched = lookup.match_records(RECORDS, "/DC2/vm/WEB-01", lookup.VM, INDEX)
    assert [record.moid for record in matched] == ["vm-102"]


def test_a_partial_path_suffix_is_enough():
    matched = lookup.match_records(RECORDS, "DC1/vm/web-01", lookup.VM, INDEX)
    assert [record.moid for record in matched] == ["vm-101"]


def test_case_insensitive_names_are_the_last_resort_and_can_be_ambiguous():
    matched = lookup.match_records(RECORDS, "wEb-01", lookup.VM, INDEX)
    assert {record.moid for record in matched} == {"vm-101", "vm-102"}


def test_no_match_returns_nothing():
    assert lookup.match_records(RECORDS, "nope", lookup.VM, INDEX) == []
    assert lookup.match_records(RECORDS, "   ", lookup.VM, INDEX) == []


def test_candidates_are_described_with_moid_and_path():
    described = lookup.describe_candidates(RECORDS[:2], INDEX)
    assert described == [
        "web-01 (vm-101, /DC1/vm/web-01)",
        "WEB-01 (vm-102, /DC2/vm/WEB-01)",
    ]


class StubServiceInstance:
    """Feeds a fixed record set to :func:`resolve_entity`."""

    def __init__(self, records):
        self.records = records


@pytest.fixture
def resolve(monkeypatch):
    def run(identifier, records=RECORDS):
        monkeypatch.setattr(lookup, "collect_properties", lambda si, vim_type, properties: records)
        return lookup.resolve_entity(StubServiceInstance(records), lookup.VM, identifier, INDEX)

    return run


def test_resolve_entity_returns_the_single_match(resolve):
    assert resolve("db-01").moid == "vm-103"


def test_resolve_entity_explains_a_miss(resolve):
    with pytest.raises(ObjectNotFoundError) as excinfo:
        resolve("ghost")
    assert "No virtual machine matches 'ghost'" in str(excinfo.value)


def test_resolve_entity_lists_ambiguous_candidates(resolve):
    with pytest.raises(AmbiguousObjectError) as excinfo:
        resolve("wEb-01")
    message = str(excinfo.value)
    assert "2 objects match" in message
    assert "managed object id" in message


def test_every_registered_kind_is_searchable():
    for name, kind in lookup.KINDS_BY_NAME.items():
        assert issubclass(kind.vim_type, vim.ManagedEntity), name
        assert "name" in kind.lookup_properties
        assert "parent" in kind.lookup_properties
