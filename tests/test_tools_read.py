"""Read-only tools exercised end to end against the fake vCenter."""

from __future__ import annotations

import pytest

from conftest import call_ok, call_tool, error_text
from vmware_mcp.config import PermissionMode


async def test_about_reports_the_endpoint_and_permission_mode(server):
    about = await call_ok(server, "vsphere_about")
    assert about["server"]["version"] == "8.0.3"
    assert about["server"]["api_type"] == "VirtualCenter"
    assert about["session_user"] == "svc-mcp@vsphere.local"
    assert about["connection"]["permission_mode"] == "read-only"
    assert "password" not in about["connection"]


async def test_list_vms_hides_templates_by_default(server):
    result = await call_ok(server, "vsphere_list_vms")
    names = [vm["name"] for vm in result["vms"]]
    assert names == ["db-01", "web-01"]
    assert result["total_matched"] == 2
    assert result["truncated"] is False


async def test_list_vms_can_return_only_templates(server):
    result = await call_ok(server, "vsphere_list_vms", only_templates=True)
    assert [vm["name"] for vm in result["vms"]] == ["ubuntu-2404-template"]
    assert result["vms"][0]["is_template"] is True


async def test_list_vms_resolves_inventory_paths(server):
    result = await call_ok(server, "vsphere_list_vms", name="web")
    vm = result["vms"][0]
    assert vm["path"] == "/DC1/vm/Tier1/web-01"
    assert vm["datacenter"] == "DC1"
    assert vm["host"] == "esxi-01.lab.local"


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"power_state": "poweredOn"}, ["web-01"]),
        ({"power_state": "poweredoff"}, ["db-01"]),
        ({"name": "DB"}, ["db-01"]),
        ({"name": "*-01"}, ["db-01", "web-01"]),
        ({"name": "web-0?"}, ["web-01"]),
        ({"guest_os": "Red Hat"}, ["db-01"]),
        ({"ip_address": "10.0.0."}, ["web-01"]),
        ({"host": "esxi-02.lab.local"}, ["db-01"]),
        ({"cluster": "Cluster-A"}, ["db-01", "web-01"]),
        ({"datacenter": "DC1"}, ["db-01", "web-01"]),
        ({"datacenter": "DC-nope"}, []),
    ],
)
async def test_list_vms_filters(server, filters, expected):
    result = await call_ok(server, "vsphere_list_vms", **filters)
    assert [vm["name"] for vm in result["vms"]] == expected


async def test_list_vms_pages_and_reports_truncation(server):
    first = await call_ok(server, "vsphere_list_vms", limit=1)
    assert [vm["name"] for vm in first["vms"]] == ["db-01"]
    assert first["truncated"] is True
    assert first["total_matched"] == 2

    second = await call_ok(server, "vsphere_list_vms", limit=1, offset=1)
    assert [vm["name"] for vm in second["vms"]] == ["web-01"]
    assert second["truncated"] is False


async def test_list_vms_rejects_a_negative_offset(server):
    result = await call_tool(server, "vsphere_list_vms", offset=-1)
    assert result.is_error
    assert "offset cannot be negative" in error_text(result)


async def test_limit_is_capped_by_max_results(server_factory, inventory):
    from conftest import make_settings
    from fake_vsphere import FakeSession
    from vmware_mcp.server import create_server
    from vmware_mcp.vsphere.client import VSphereClient

    settings = make_settings(max_results=1, default_page_size=1)
    server = create_server(settings, client=VSphereClient(settings, session=FakeSession(inventory)))
    result = await call_ok(server, "vsphere_list_vms", limit=500)
    assert result["limit"] == 1
    assert result["returned"] == 1


async def test_get_vm_by_name_moid_uuid_and_path(server):
    for identifier in (
        "web-01",
        "vm-101",
        "4213vm-101-bios",
        "5013vm-101-instance",
        "/DC1/vm/Tier1/web-01",
    ):
        result = await call_ok(server, "vsphere_get_vm", vm=identifier)
        assert result["vm"]["moid"] == "vm-101", identifier


async def test_get_vm_is_case_insensitive_on_names(server):
    result = await call_ok(server, "vsphere_get_vm", vm="WEB-01")
    assert result["vm"]["name"] == "web-01"


async def test_get_vm_reports_annotation_and_hardware_version(server):
    vm = (await call_ok(server, "vsphere_get_vm", vm="web-01"))["vm"]
    assert vm["annotation"] == "notes for web-01"
    assert vm["hardware_version"] == "vmx-19"
    assert vm["vmx_path"] == "[ds-nvme] web-01/web-01.vmx"
    assert vm["datastore_moids"] == ["datastore-21"]


async def test_unknown_vm_explains_the_accepted_identifiers(server):
    result = await call_tool(server, "vsphere_get_vm", vm="nope-99")
    assert result.is_error
    message = error_text(result)
    assert "No virtual machine matches 'nope-99'" in message
    assert "inventory path" in message


async def test_ambiguous_names_list_the_candidates(server, inventory):
    from pyVmomi import vim

    inventory.add(
        "vm-999",
        vim.VirtualMachine,
        name="web-01",
        parent=vim.Folder("group-v3", inventory.stub),
    )
    result = await call_tool(server, "vsphere_get_vm", vm="web-01")
    assert result.is_error
    message = error_text(result)
    assert "2 objects match 'web-01'" in message
    assert "vm-101" in message and "vm-999" in message


async def test_list_hosts_reports_utilisation(server):
    result = await call_ok(server, "vsphere_list_hosts")
    assert [host["name"] for host in result["hosts"]] == [
        "esxi-01.lab.local",
        "esxi-02.lab.local",
    ]
    host = result["hosts"][0]
    assert host["cluster"] == "Cluster-A"
    assert host["cpu_total_mhz"] == 48000
    assert host["memory_gib"] == 256.0
    assert host["esxi_version"] == "8.0.3"


async def test_list_hosts_filters_by_cluster_and_state(server):
    assert (await call_ok(server, "vsphere_list_hosts", cluster="Cluster-B"))["hosts"] == []
    connected = await call_ok(server, "vsphere_list_hosts", connection_state="CONNECTED")
    assert len(connected["hosts"]) == 2


async def test_get_host_includes_attached_objects(server):
    host = (await call_ok(server, "vsphere_get_host", host="esxi-01.lab.local"))["host"]
    assert host["vm_count"] == 2
    assert host["hostname"] == "esxi-01"
    assert host["domain"] == "lab.local"
    assert host["bios_version"] == "1.9.2"


async def test_list_clusters_reports_drs_and_ha(server):
    clusters = (await call_ok(server, "vsphere_list_clusters"))["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["name"] == "Cluster-A"
    assert clusters[0]["drs_enabled"] is True
    assert clusters[0]["ha_enabled"] is True
    assert clusters[0]["datacenter"] == "DC1"


async def test_list_datacenters(server):
    result = await call_ok(server, "vsphere_list_datacenters")
    assert [dc["name"] for dc in result["datacenters"]] == ["DC1"]


async def test_list_resource_pools(server):
    pools = (await call_ok(server, "vsphere_list_resource_pools"))["resource_pools"]
    assert pools[0]["name"] == "Resources"
    assert pools[0]["owner"] == "Cluster-A"
    assert pools[0]["vm_count"] == 1


async def test_list_datastores_aggregates_capacity(server):
    result = await call_ok(server, "vsphere_list_datastores")
    assert [ds["name"] for ds in result["datastores"]] == ["ds-nfs", "ds-nvme"]
    assert result["aggregate"]["capacity_gib"] == 6144.0
    nfs = result["datastores"][0]
    assert nfs["type"] == "NFS"
    assert nfs["used_percent"] == 90.0


async def test_list_datastores_filters_on_utilisation(server):
    result = await call_ok(server, "vsphere_list_datastores", min_used_percent=80)
    assert [ds["name"] for ds in result["datastores"]] == ["ds-nfs"]


async def test_list_networks_includes_distributed_portgroup_details(server):
    networks = (await call_ok(server, "vsphere_list_networks"))["networks"]
    by_name = {network["name"]: network for network in networks}
    assert by_name["VM Network"]["kind"] == "standard-portgroup"
    dvpg = by_name["dvpg-prod"]
    assert dvpg["kind"] == "distributed-portgroup"
    assert dvpg["vlan"] == {"mode": "access", "vlan_id": 120}
    assert dvpg["dvs"] == "DSwitch"
    assert dvpg["port_count"] == 128


async def test_search_inventory_spans_object_types(server):
    result = await call_ok(server, "vsphere_search_inventory", query="01")
    kinds = {match["type"] for match in result["matches"]}
    assert {"vm", "host"} <= kinds
    web = next(match for match in result["matches"] if match["name"] == "web-01")
    assert web["moid"] == "vm-101"
    assert web["path"] == "/DC1/vm/Tier1/web-01"


async def test_search_inventory_can_be_scoped_to_one_type(server):
    result = await call_ok(server, "vsphere_search_inventory", query="*", types=["datastore"])
    assert {match["name"] for match in result["matches"]} == {"ds-nvme", "ds-nfs"}


async def test_search_inventory_rejects_unknown_types(server):
    result = await call_tool(server, "vsphere_search_inventory", query="x", types=["toaster"])
    assert result.is_error
    assert "Unknown object type(s): toaster" in error_text(result)


async def test_vm_summary_by_host_computes_overcommit(server):
    result = await call_ok(server, "vsphere_get_vm_summary_by_host")
    by_host = {entry["host"]: entry for entry in result["hosts"]}
    assert by_host["esxi-01.lab.local"]["vm_count"] == 1
    assert by_host["esxi-01.lab.local"]["powered_on_vm_count"] == 1
    assert by_host["esxi-01.lab.local"]["allocated_vcpus"] == 4
    assert by_host["esxi-01.lab.local"]["vcpu_overcommit_ratio"] == 0.17
    # db-01 is powered off, so it allocates nothing.
    assert by_host["esxi-02.lab.local"]["allocated_vcpus"] == 0


async def test_read_only_mode_still_serves_every_read_tool(server_factory):
    server = server_factory(PermissionMode.READ_ONLY)
    for name in ("vsphere_list_vms", "vsphere_list_hosts", "vsphere_list_datastores"):
        result = await call_tool(server, name)
        assert not result.is_error, name
