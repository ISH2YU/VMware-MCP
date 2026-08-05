# VMware MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server for VMware vSphere. It gives an
AI assistant read access to your vCenter Server or standalone ESXi host — inventory, performance
counters, events and alarms — and, when you explicitly allow it, the ability to power VMs on and
off, take snapshots, clone, reconfigure and migrate them.

It talks to vSphere with [pyVmomi](https://github.com/vmware/pyvmomi), the official VMware Python
SDK, over the same vSphere Web Services API the vSphere Client uses. No agent, no appliance, and
nothing to install on your hosts.

> **Read-only by default.** Out of the box the server refuses every operation that would change
> your environment. Enabling writes is a deliberate, separate step.

## Contents

- [Quick start](#quick-start)
- [Configuring an MCP client](#configuring-an-mcp-client)
- [Permission modes](#permission-modes)
- [Tools](#tools)
- [Resources and prompts](#resources-and-prompts)
- [Configuration reference](#configuration-reference)
- [Referring to objects](#referring-to-objects)
- [Security notes](#security-notes)
- [Development](#development)

## Quick start

Requires Python 3.10 or newer and network access to port 443 on vCenter or ESXi.

```bash
git clone https://github.com/ISH2YU/VMware-MCP.git
cd VMware-MCP
pip install -e .
```

Set the connection details and check that they work before wiring anything up to an AI client:

```bash
export VMWARE_HOST=vcenter.example.com
export VMWARE_USERNAME='svc-mcp@vsphere.local'
export VMWARE_PASSWORD='...'

vmware-mcp --check
```

`--check` logs in, prints the vCenter version, the account it authenticated as and the number of
inventory objects it can see, then exits:

```json
{
  "endpoint": "vcenter.example.com:443",
  "permission_mode": "read-only",
  "verify_ssl": true,
  "authenticated_as": "svc-mcp@vsphere.local",
  "server": {
    "name": "VMware vCenter Server 8.0.3 build-24022515",
    "product": "VMware vCenter Server",
    "version": "8.0.3",
    "build": "24022515",
    "api_version": "8.0.3.0",
    "api_type": "VirtualCenter",
    "os_type": "linux-x64",
    "vendor": "VMware, Inc.",
    "instance_uuid": "aaaa-bbbb-cccc",
    "license_product": "VMware VirtualCenter Server"
  },
  "inventory_objects_indexed": 214
}
```

If the TLS handshake fails, either point `VMWARE_CA_BUNDLE` at your vCenter's CA certificate or, for
a lab, set `VMWARE_VERIFY_SSL=false`.

Then run the server. It speaks stdio by default, which is what desktop MCP clients expect:

```bash
vmware-mcp
```

## Configuring an MCP client

### Cursor

Add to `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "vmware": {
      "command": "vmware-mcp",
      "env": {
        "VMWARE_HOST": "vcenter.example.com",
        "VMWARE_USERNAME": "svc-mcp@vsphere.local",
        "VMWARE_PASSWORD": "...",
        "VMWARE_PERMISSION_MODE": "read-only"
      }
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "vmware": {
      "command": "vmware-mcp",
      "env": {
        "VMWARE_HOST": "vcenter.example.com",
        "VMWARE_USERNAME": "svc-mcp@vsphere.local",
        "VMWARE_PASSWORD": "..."
      }
    }
  }
}
```

### Over HTTP

To run the server once and share it, use the streamable HTTP transport:

```bash
vmware-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The endpoint has no authentication of its own, so bind it to localhost or put it behind a proxy
that does.

## Permission modes

`VMWARE_PERMISSION_MODE` decides how much damage the server can do. Modes are cumulative.

| Mode | What it allows |
| --- | --- |
| `read-only` (default) | Inventory, monitoring, events, alarms and performance counters. Every mutating tool refuses with an explanation. |
| `write` | Adds power operations, snapshot creation, clone, reconfigure and migrate. |
| `destructive` | Adds deleting VMs and reverting or deleting snapshots. |

The mode is enforced server-side, before any call reaches vCenter, and it is stated in the server
instructions so the model knows what it can attempt. Deleting a VM additionally requires an explicit
`confirm=true` argument and refuses while the VM is powered on.

Belt and braces: give the service account only the vCenter privileges it needs. The permission mode
protects against a confused model, not against a compromised one — vCenter roles are the real
boundary.

## Tools

All 27 tools are prefixed `vsphere_`.

### Inventory

| Tool | Purpose |
| --- | --- |
| `vsphere_about` | Product, version, API type, authenticated user and the active permission mode. |
| `vsphere_list_datacenters` | Every datacenter. |
| `vsphere_list_clusters` | Clusters with capacity, DRS and HA configuration. |
| `vsphere_list_hosts` | ESXi hosts with hardware, version and live CPU/memory utilisation. |
| `vsphere_get_host` | One host in full, including its VMs, datastores and networks. |
| `vsphere_list_resource_pools` | Reservations, limits and current usage. |
| `vsphere_search_inventory` | Search by name across object types when you don't know what a name refers to. |

### Virtual machines

| Tool | Purpose |
| --- | --- |
| `vsphere_list_vms` | VMs filtered by name, power state, datacenter, cluster, host, guest OS or IP. |
| `vsphere_get_vm` | One VM in full: hardware, disks, NICs, guest networking and filesystems, snapshots. |
| `vsphere_get_vm_summary_by_host` | VM counts and vCPU/memory overcommitment per host. |

### Storage and networking

| Tool | Purpose |
| --- | --- |
| `vsphere_list_datastores` | Capacity, free space, provisioned space and over-provisioning. |
| `vsphere_list_networks` | Standard port groups, distributed port groups (with VLAN and switch) and opaque networks. |

### Monitoring

| Tool | Purpose |
| --- | --- |
| `vsphere_list_tasks` | Recent tasks, optionally scoped to an object. |
| `vsphere_get_task` | Poll a single task by id. |
| `vsphere_list_running_tasks` | What vCenter is doing right now. |
| `vsphere_list_events` | The audit trail: logins, changes, HA actions, hardware problems. |
| `vsphere_list_alarms` | Every currently triggered alarm, red first. |
| `vsphere_get_performance` | CPU, memory, disk and network counters for a VM or host, summarised. |

### Power, snapshots and lifecycle

| Tool | Mode | Purpose |
| --- | --- | --- |
| `vsphere_change_vm_power_state` | `write` | Power on/off, suspend, reset, or ask the guest to shut down or reboot. |
| `vsphere_list_snapshots` | read | Snapshot tree plus a flat list with paths. |
| `vsphere_create_snapshot` | `write` | Take a snapshot, optionally with memory or quiesced. |
| `vsphere_revert_to_snapshot` | `destructive` | Revert, discarding later changes. |
| `vsphere_delete_snapshot` | `destructive` | Delete one snapshot, a subtree, or all of them. |
| `vsphere_clone_vm` | `write` | Clone a VM or deploy from a template. |
| `vsphere_reconfigure_vm` | `write` | Change vCPU count, cores per socket, memory or notes. |
| `vsphere_migrate_vm` | `write` | vMotion, storage vMotion, or both. |
| `vsphere_delete_vm` | `destructive` | Delete a VM and its disks. Needs `confirm=true`. |

Long operations (clone, migrate, snapshot) accept `wait=false` and return a `task_id` you can poll
with `vsphere_get_task`. When waiting, progress is streamed to the client as MCP progress
notifications.

## Resources and prompts

Two resources:

- `vsphere://inventory/summary` — counts and totals for the whole environment, useful as ambient
  context.
- `vsphere://vm/{identifier}` — full detail for one VM, by name, moid, UUID or path.

Two prompts:

- `troubleshoot_vm(vm)` — a diagnostic walkthrough for a slow, stuck or unreachable VM: performance
  counters for the VM and its host, recent events and tasks, alarms, snapshot age and datastore
  pressure.
- `capacity_report(scope)` — cluster and host utilisation, overcommitment ratios, datastores nearing
  full, and a prioritised list of recommendations.

## Configuration reference

Every setting is an environment variable; the flags shown override them.

| Variable | Default | Description |
| --- | --- | --- |
| `VMWARE_HOST` | *required* | vCenter Server or ESXi hostname or IP. Also `VSPHERE_HOST`. |
| `VMWARE_USERNAME` | *required* | vSphere account. Also `VMWARE_USER`, `VSPHERE_USER`. |
| `VMWARE_PASSWORD` | *required* | Password. Also `VSPHERE_PASSWORD`. |
| `VMWARE_PORT` | `443` | API port. |
| `VMWARE_VERIFY_SSL` | `true` | Verify the TLS certificate. |
| `VMWARE_INSECURE` | `false` | Shorthand for disabling verification. |
| `VMWARE_CA_BUNDLE` | — | CA certificate bundle to trust instead of the system store. |
| `VMWARE_PERMISSION_MODE` | `read-only` | `read-only`, `write` or `destructive`. |
| `VMWARE_CONNECT_TIMEOUT` | `30` | HTTP connection timeout in seconds. |
| `VMWARE_TASK_TIMEOUT` | `600` | How long to wait for a vSphere task before handing back its id. |
| `VMWARE_MAX_RESULTS` | `500` | Hard cap on items returned by any listing. |
| `VMWARE_DEFAULT_PAGE_SIZE` | `100` | Page size when a tool call omits `limit`. |
| `VMWARE_CACHE_TTL` | `60` | Seconds to cache the inventory tree used for path resolution. |
| `VMWARE_MAX_CONCURRENCY` | `8` | Maximum simultaneous calls to vCenter. |
| `VMWARE_LOG_LEVEL` | `INFO` | Log level. Logs go to stderr. |
| `VMWARE_TRANSPORT` | `stdio` | `stdio`, `streamable-http` or `sse`. |

Command line flags: `--vsphere-host`, `--vsphere-port`, `--username`, `--insecure`, `--ca-bundle`,
`--permission-mode`, `--transport`, `--host`, `--port`, `--log-level`, `--check`. Run
`vmware-mcp --help` for details. There is deliberately no `--password` flag; passwords on a command
line end up in the process list and shell history.

## Referring to objects

Every tool that takes a VM, host, datastore, network or cluster accepts any of:

- the object name — `web-01`, matched case-insensitively
- a glob, where a name filter is accepted — `web-*`, `db-0?`
- the managed object id — `vm-1024`, `host-42`
- a UUID, for VMs and hosts — BIOS or instance UUID
- the inventory path — `/Prod/vm/Tier1/web-01`, or any suffix of it

When several objects match, the tool lists the candidates with their managed object ids rather than
picking one. Listings are paginated and report `truncated`, so a model can tell the difference
between "that's all of them" and "there are more".

## Security notes

- **Create a dedicated service account** in vSphere rather than reusing an administrator. Grant it a
  read-only role for the default mode; add only the specific privileges you need if you enable
  writes.
- **The password is never returned** by any tool. `vsphere_about` reports the connection settings
  with the password omitted.
- **Certificate verification is on by default.** Disabling it is logged as a warning at startup.
- **Anything an MCP client can call, a model can call.** Permission modes and the `confirm` flag on
  deletion exist because prompt injection through, for example, a VM annotation is a real risk. Keep
  the server in `read-only` unless a task genuinely needs more.
- **Secrets in client config files** are stored in plain text by most MCP clients. Prefer a secret
  manager or environment variables where your client supports them.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                     # 200+ tests, no vCenter required
ruff check src tests       # lint
ruff format src tests      # format
mypy                       # type check
```

The test suite runs against an in-memory vCenter double (`tests/fake_vsphere.py`) that implements
the two seams the server actually depends on: pyVmomi's SOAP stub and the PropertyCollector.
Everything above those seams — the real client, the real property specs, the real mappers and the
real tools — runs unmodified, so tests exercise the same code paths that talk to a live vCenter.

### How it is put together

```
src/vmware_mcp/
├── config.py          Environment parsing and permission modes
├── errors.py          Error types, all with client-safe messages
├── server.py          MCPServer assembly, resources and prompts
├── cli.py             Argument parsing, --check, transports
├── vsphere/
│   ├── session.py     Connection, TLS, reconnect on session expiry
│   ├── client.py      Async facade; runs pyVmomi on a bounded thread pool
│   ├── query.py       PropertyCollector batching and inventory paths
│   ├── mappers.py     Pure vSphere-to-JSON translation
│   ├── lookup.py      Name/moid/UUID/path resolution
│   ├── tasks.py       Task polling with progress reporting
│   ├── perf.py        Performance counter queries
│   └── monitoring.py  Events, tasks and alarms
└── tools/             One module per area of vSphere
```

Two decisions worth knowing about:

**Everything reads through the PropertyCollector.** Touching managed object attributes one at a time
costs a round trip each, which is unusable against a vCenter with thousands of VMs. A listing here
is a single `RetrievePropertiesEx` call regardless of how many objects come back.

**pyVmomi is synchronous, MCP is not.** Blocking calls run on a thread pool bounded by
`VMWARE_MAX_CONCURRENCY`, and vSphere tasks are polled from the event loop rather than blocking a
thread, so progress can be streamed and a cancelled request does not strand a worker.

## License

MIT. See [LICENSE](LICENSE).
