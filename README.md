# VMware MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant drive
**VMware Workstation, Fusion or Player on your own machine** — list VMs, power them on and off,
take snapshots, clone a golden Windows image into a pile of disposable test VMs, copy installers
in, run commands inside the guest, and tear everything down again.

It talks to the local hypervisor through VMware's `vmrun` command-line tool. No vCenter, no
appliance, nothing to install on the guests beyond VMware Tools (which you want anyway).

There is also an optional **vSphere** backend for vCenter Server / ESXi if you need it later; the
default is local.

> **Read-only by default.** Out of the box the server refuses every operation that would change a
> VM. Enabling writes (cloning, power, guest commands) is a deliberate step.

## Why this exists

The workflow it is built for:

1. Keep one golden Windows VM (say `win11-golden`) with VMware Tools, updates and a clean
   `golden` snapshot.
2. Ask the AI to spin up N linked clones from that snapshot for a test run.
3. Have it wait until Tools is up, copy an installer in, run a silent install, capture the result
   (and a screenshot if it fails).
4. Revert or delete the clones when you are done.

That is what `vmware_clone_many`, `vmware_wait_for_guest`, `vmware_copy_to_guest`,
`vmware_run_command` and `vmware_revert_snapshot` are for.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configure an MCP client](#configure-an-mcp-client)
- [Permission modes](#permission-modes)
- [Typical workflows](#typical-workflows)
- [Tools](#tools)
- [Resources and prompts](#resources-and-prompts)
- [Configuration reference](#configuration-reference)
- [Referring to VMs](#referring-to-vms)
- [Security notes](#security-notes)
- [Optional: vSphere backend](#optional-vsphere-backend)
- [Development](#development)

## Requirements

- **VMware Workstation Pro**, **VMware Fusion** or **VMware Player/Workstation Player** installed
  locally, with the `vmrun` tool available (it ships with all of them).
- **Python 3.10+**
- For guest commands (run installer, copy files, etc.): **VMware Tools** inside the guest, plus a
  guest username/password the server can use.

`vmrun` is usually on `PATH`. If not, set `VMWARE_VMRUN_PATH` (see below for the usual install
locations).

## Quick start

```bash
git clone https://github.com/ISH2YU/VMware-MCP.git
cd VMware-MCP
pip install -e .
```

Point it at the folder where your VMs live and check that it can see them:

```bash
# Windows example
set VMWARE_VM_DIRS=%USERPROFILE%\Documents\Virtual Machines
set VMWARE_PERMISSION_MODE=write
set VMWARE_GUEST_USERNAME=Administrator
set VMWARE_GUEST_PASSWORD=...

vmware-mcp --check
```

```bash
# macOS / Linux example
export VMWARE_VM_DIRS="$HOME/Virtual Machines.localized"   # Fusion default; adjust for Workstation
export VMWARE_PERMISSION_MODE=write
export VMWARE_GUEST_USERNAME=Administrator
export VMWARE_GUEST_PASSWORD='...'

vmware-mcp --check
```

`--check` finds `vmrun`, scans your VM directories and prints a summary:

```json
{
  "backend": "workstation",
  "product": "ws",
  "vmrun": "C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe",
  "vmrun_version": "vmrun version 1.17.0 build-...",
  "vm_directories": ["C:\\Users\\you\\Documents\\Virtual Machines"],
  "vm_count": 4,
  "running_count": 1,
  "guest_credentials_configured": true,
  "connection": {
    "backend": "workstation",
    "permission_mode": "write",
    "product": "ws"
  }
}
```

Then run the server (stdio is what desktop MCP clients expect):

```bash
vmware-mcp
```

## Configure an MCP client

### Cursor

Add to `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "vmware": {
      "command": "vmware-mcp",
      "env": {
        "VMWARE_VM_DIRS": "C:\\Users\\you\\Documents\\Virtual Machines",
        "VMWARE_PERMISSION_MODE": "write",
        "VMWARE_GUEST_USERNAME": "Administrator",
        "VMWARE_GUEST_PASSWORD": "..."
      }
    }
  }
}
```

On a Mac with Fusion, set `"VMWARE_PRODUCT": "fusion"` and point `VMWARE_VM_DIRS` at
`~/Virtual Machines.localized`.

### Claude Desktop

Same shape in `claude_desktop_config.json`.

### Over HTTP

```bash
vmware-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Bind to localhost (or put it behind an authenticating proxy). The HTTP endpoint has no auth of its
own.

## Permission modes

`VMWARE_PERMISSION_MODE` decides how much the server can do. Modes are cumulative.

| Mode | What it allows |
| --- | --- |
| `read-only` (default) | List and inspect VMs, list snapshots, wait for guest/Tools. Everything that changes a VM refuses. |
| `write` | Adds power on/off, create snapshots, clone (including batch), reconfigure, screenshots, and all guest operations (run command, copy files). |
| `destructive` | Adds deleting VMs and reverting or deleting snapshots. |

Deleting a VM additionally requires `confirm=true` and refuses while the VM is powered on.

For the "spin up Windows test VMs" workflow you want at least `write`. Use `destructive` when you
also want the AI to revert or delete clones when a run finishes.

## Typical workflows

### Spin up N Windows test VMs from a golden image

Keep a template VM with a clean snapshot named `golden`. Then ask the assistant something like:

> Clone 5 linked VMs from `win11-golden` snapshot `golden`, name them `web-test-01` …
> `web-test-05`, start them headless, wait until each has Tools and an IP, and list the IPs.

Under the hood that is:

1. `vmware_get_vm` / `vmware_list_snapshots` on the golden image
2. `vmware_clone_many` with `clone_type=linked`, `snapshot=golden`, `start=true`
3. `vmware_wait_for_guest` per clone
4. Report names, paths and IPs

There is a built-in prompt for this: `spin_up_test_vms`.

### Install and test a package on one Windows VM

> On `web-test-01`, copy `C:\Builds\app.msi` to `C:\Temp\app.msi`, install it quietly, and tell me
> if it worked.

1. `vmware_wait_for_guest`
2. `vmware_run_command` to ensure `C:\Temp` exists
3. `vmware_copy_to_guest`
4. `vmware_run_command` with `msiexec.exe /i C:\Temp\app.msi /qn`
5. `vmware_screenshot` if the exit code is non-zero

Built-in prompt: `run_windows_test`.

### Reset everything between runs

> Revert every VM named `web-test-*` back to snapshot `golden`.

Built-in prompt: `reset_test_vms`. Or delete them with `vmware_delete_vm` (needs `destructive` +
`confirm=true`).

## Tools

All local tools are prefixed `vmware_`.

### Inventory

| Tool | Purpose |
| --- | --- |
| `vmware_about` | Product, `vmrun` path, VM counts, guest-credential status, permission mode. Call this first. |
| `vmware_list_vms` | List VMs under the configured directories. Filter by name, guest OS, family, running/off. |
| `vmware_get_vm` | One VM in full: CPU, memory, disks, NICs, Tools state, IP, snapshots. |
| `vmware_list_running` | `.vmx` paths of every VM currently powered on. |

### Power

| Tool | Mode | Purpose |
| --- | --- | --- |
| `vmware_power_vm` | `write` | `start`, `stop`, `reset`, `suspend`, `pause`, `unpause`, `hard_stop`, `hard_reset`. Soft stop/reset ask the guest via Tools; hard_* pull the plug. `gui=false` (default) starts headless. |

### Snapshots

| Tool | Mode | Purpose |
| --- | --- | --- |
| `vmware_list_snapshots` | read | Snapshot names on a VM. |
| `vmware_create_snapshot` | `write` | Take a snapshot (e.g. `golden` on the template). |
| `vmware_revert_snapshot` | `destructive` | Roll a VM back, discarding later changes. |
| `vmware_delete_snapshot` | `destructive` | Delete a snapshot, optionally with children. |

### Clone / reconfigure / delete

| Tool | Mode | Purpose |
| --- | --- | --- |
| `vmware_clone_vm` | `write` | Clone one VM. Prefer `clone_type=linked` + `snapshot=...` for test labs. |
| `vmware_clone_many` | `write` | Clone N VMs as `{prefix}-01` … `{prefix}-N` (max 50). Continues past individual failures. |
| `vmware_reconfigure_vm` | `write` | Change display name, CPU, memory or notes. VM must be powered off; edits the `.vmx`. |
| `vmware_delete_vm` | `destructive` | Delete a VM and its files. Needs `confirm=true`. |
| `vmware_screenshot` | `write` | Capture a PNG of a running VM's display. |

### Guest operations (need Tools + credentials)

| Tool | Mode | Purpose |
| --- | --- | --- |
| `vmware_wait_for_guest` | read | Block until Tools is running, optionally until an IP is assigned. |
| `vmware_run_command` | `write` | Run a program in the guest; returns exit code, stdout and stderr. |
| `vmware_run_script` | `write` | Upload a short script and run it (handy for multi-line PowerShell). |
| `vmware_copy_to_guest` | `write` | Copy a host file into the guest. |
| `vmware_copy_from_guest` | `write` | Copy a guest file out to the host. |
| `vmware_list_guest_directory` | read | List a directory inside the guest. |

Guest credentials come from `VMWARE_GUEST_USERNAME` / `VMWARE_GUEST_PASSWORD`, or can be passed per
call. On Windows, `program` is usually `cmd.exe` or `powershell.exe`.

## Resources and prompts

Resources:

- `vmware://vms` — every discovered local VM
- `vmware://vm/{identifier}` — full detail for one VM

Prompts:

- `spin_up_test_vms(template, count, name_prefix, snapshot)` — clone N disposable VMs from a golden image
- `run_windows_test(vm, installer_host_path, guest_path)` — copy, install, report
- `reset_test_vms(name_prefix, snapshot)` — revert a batch back to clean

## Configuration reference

| Variable | Default | Description |
| --- | --- | --- |
| `VMWARE_BACKEND` | auto | `workstation` (default) or `vsphere`. Auto: vSphere if `VMWARE_HOST` is set, else local. |
| `VMWARE_VM_DIRS` | platform default | Directories to scan for `.vmx` files. Use the OS path separator to list several. |
| `VMWARE_VMRUN_PATH` | auto-detect | Full path to `vmrun` if it is not on `PATH`. |
| `VMWARE_PRODUCT` | auto | `ws` (Workstation), `fusion`, or `player`. Auto: `fusion` on macOS, `ws` elsewhere. |
| `VMWARE_GUEST_USERNAME` | — | Default guest OS username for guest operations. |
| `VMWARE_GUEST_PASSWORD` | — | Default guest OS password. |
| `VMWARE_PERMISSION_MODE` | `read-only` | `read-only`, `write` or `destructive`. |
| `VMWARE_COMMAND_TIMEOUT` | `120` | Seconds before a `vmrun` call is killed. Raise this for big full clones. |
| `VMWARE_GUEST_TIMEOUT` | `300` | Seconds for guest program runs and file copies. |
| `VMWARE_BOOT_TIMEOUT` | `300` | Seconds to wait for Tools / guest IP after power on. |
| `VMWARE_MAX_OUTPUT_BYTES` | `100000` | Cap on captured guest stdout/stderr. |
| `VMWARE_MAX_RESULTS` | `500` | Hard cap on items returned by any listing. |
| `VMWARE_DEFAULT_PAGE_SIZE` | `100` | Page size when a tool call omits `limit`. |
| `VMWARE_LOG_LEVEL` | `INFO` | Logs go to stderr (stdout is the MCP protocol). |
| `VMWARE_TRANSPORT` | `stdio` | `stdio`, `streamable-http` or `sse`. |

### Where `vmrun` usually lives

| Platform | Typical path |
| --- | --- |
| Windows | `C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe` |
| macOS | `/Applications/VMware Fusion.app/Contents/Public/vmrun` |
| Linux | `/usr/bin/vmrun` |

### Where VMs usually live

| Platform | Typical folder |
| --- | --- |
| Windows | `%USERPROFILE%\Documents\Virtual Machines` |
| macOS (Fusion) | `~/Virtual Machines.localized` |
| Linux | `~/vmware` |

Command-line flags: `--backend`, `--vmrun`, `--product`, `--vm-dir` (repeatable), `--guest-user`,
`--permission-mode`, `--transport`, `--host`, `--port`, `--log-level`, `--check`. Run
`vmware-mcp --help` for details. There is deliberately no password flag — passwords on a command
line end up in the process list and shell history.

## Referring to VMs

Every tool that takes a VM accepts any of:

- the display name — `win11-golden` (case-insensitive)
- a glob, where a name filter is accepted — `web-test-*`
- the full `.vmx` path
- the directory / stem name
- the BIOS UUID from the `.vmx`

When several VMs match, the tool lists the candidates with their paths rather than picking one.

## Security notes

- **Start in `read-only`.** Only raise the permission mode when you actually want the AI to change
  VMs. Prompt injection (through a VM annotation, a web page the model reads, etc.) is a real risk.
- **Guest credentials are powerful.** The account in `VMWARE_GUEST_USERNAME` can run arbitrary
  commands inside every VM the server can see. Use a dedicated local admin on the golden image, not
  your day-to-day account, and prefer `write` over `destructive` until you need revert/delete.
- **Passwords never appear in tool output.** `vmware_about` reports whether guest credentials are
  configured, not what they are.
- **Linked clones share disks with the parent.** Do not delete or heavily modify the golden image
  while linked clones still depend on it.
- **Secrets in MCP client config files** are stored in plain text by most clients. Prefer a secret
  manager or environment variables where your client supports them.

## Optional: vSphere backend

If you later want to point this at a vCenter Server or ESXi host instead of local VMs:

```bash
export VMWARE_BACKEND=vsphere          # or just set VMWARE_HOST — that selects vSphere automatically
export VMWARE_HOST=vcenter.example.com
export VMWARE_USERNAME='svc-mcp@vsphere.local'
export VMWARE_PASSWORD='...'
export VMWARE_PERMISSION_MODE=read-only
vmware-mcp --check
```

That backend exposes a separate `vsphere_*` tool set (inventory, clusters, hosts, datastores,
alarms, performance counters, vMotion, …). See the tool descriptions once connected. Local and
vSphere are not active at the same time — pick one backend per process.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"

pytest                  # no VMware install required
ruff check src tests
ruff format src tests
mypy
```

The local-backend tests run against a fake `vmrun` and real `.vmx` fixtures, so the real client,
`.vmx` parser, discovery and tools all execute unmodified. The vSphere backend has its own
in-memory vCenter double and suite.

### Layout

```
src/vmware_mcp/
├── config.py              Backend detection, settings, permission modes
├── server.py              MCPServer assembly, resources, prompts
├── cli.py                 Argument parsing, --check, transports
├── workstation/           Local Workstation / Fusion / Player
│   ├── vmrun.py           Locate and run vmrun
│   ├── vmx.py             Parse / edit .vmx files
│   ├── discovery.py       Find VMs and resolve names/paths/UUIDs
│   ├── guest.py           Guest commands, file copy, Tools/IP wait
│   └── client.py          High-level async API
├── vsphere/               Optional vCenter / ESXi backend (pyVmomi)
└── tools/
    ├── workstation/       vmware_* tools
    └── vsphere/           vsphere_* tools
```

## License

MIT. See [LICENSE](LICENSE).
