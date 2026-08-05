# VMware MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server for **VMware Workstation**
(also works with Fusion and Player on the same machine).

It lets an AI assistant manage the VMs on your PC: list them, power them on/off, take snapshots,
clone a golden Windows image into many disposable test VMs, copy installers in, run commands
inside the guest, and tear them down when you are done.

It uses VMware's built-in `vmrun` tool. No vCenter. No ESXi. No extra agents.

> **Read-only by default.** Nothing that changes a VM works until you set
> `VMWARE_PERMISSION_MODE=write`.

## What you need

1. **VMware Workstation** installed (Fusion or Player also fine)
2. **Python 3.10+**
3. For running commands inside Windows VMs: **VMware Tools** in the guest, plus a guest
   username/password

## Install

```bash
git clone https://github.com/ISH2YU/VMware-MCP.git
cd VMware-MCP
pip install -e .
```

## Configure

Set these (PowerShell example — adjust the path to where your VMs live):

```powershell
$env:VMWARE_VM_DIRS = "$env:USERPROFILE\Documents\Virtual Machines"
$env:VMWARE_PERMISSION_MODE = "write"
$env:VMWARE_GUEST_USERNAME = "Administrator"
$env:VMWARE_GUEST_PASSWORD = "YourGuestPassword"
```

macOS / Linux:

```bash
export VMWARE_VM_DIRS="$HOME/vmware"          # or ~/Virtual Machines.localized on Fusion
export VMWARE_PERMISSION_MODE=write
export VMWARE_GUEST_USERNAME=Administrator
export VMWARE_GUEST_PASSWORD='...'
```

Check that it can see your VMs:

```bash
vmware-mcp --check
```

You should get JSON like:

```json
{
  "backend": "workstation",
  "product": "ws",
  "vmrun": "C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe",
  "vm_count": 3,
  "running_count": 0,
  "guest_credentials_configured": true
}
```

If it says it cannot find `vmrun`, set `VMWARE_VMRUN_PATH` to the full path of `vmrun.exe`
(usually under `C:\Program Files (x86)\VMware\VMware Workstation\`).

## Add it to Cursor

In `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "vmware": {
      "command": "vmware-mcp",
      "env": {
        "VMWARE_VM_DIRS": "C:\\Users\\YOU\\Documents\\Virtual Machines",
        "VMWARE_PERMISSION_MODE": "write",
        "VMWARE_GUEST_USERNAME": "Administrator",
        "VMWARE_GUEST_PASSWORD": "YourGuestPassword"
      }
    }
  }
}
```

Restart Cursor. You should see the `vmware_*` tools available to the agent.

## How to use it for Windows test VMs

### 1. Prepare a golden image (once)

In Workstation, create a Windows VM, install VMware Tools, Windows updates, and anything else
you always want. Take a snapshot named `golden`. Keep this VM powered off when cloning from it.

### 2. Ask the AI to spin up test VMs

Example prompts:

> Clone 5 linked VMs from `win11-golden` using snapshot `golden`, name them `web-test-01` through
> `web-test-05`, start them headless, wait until each has an IP, and list the IPs.

> On `web-test-01`, copy `C:\Builds\app.msi` to `C:\Temp\app.msi`, install it quietly with msiexec,
> and tell me the exit code. Screenshot if it fails.

> Revert every VM named `web-test-*` back to snapshot `golden`.

There are also built-in prompts: `spin_up_test_vms`, `run_windows_test`, `reset_test_vms`.

### 3. What the tools do

| Tool | What it does |
| --- | --- |
| `vmware_about` | Product, vmrun path, how many VMs it sees, permission mode |
| `vmware_list_vms` | List VMs (filter by name, Windows/Linux, running/off) |
| `vmware_get_vm` | Full detail: CPU, RAM, disks, Tools, IP, snapshots |
| `vmware_list_running` | Which VMs are powered on right now |
| `vmware_power_vm` | start / stop / reset / suspend / pause (soft or hard) |
| `vmware_list_snapshots` | Snapshot names |
| `vmware_create_snapshot` | Take a snapshot (e.g. `golden`) |
| `vmware_revert_snapshot` | Roll a VM back |
| `vmware_delete_snapshot` | Delete a snapshot |
| `vmware_clone_vm` | Clone one VM (`linked` recommended) |
| `vmware_clone_many` | Clone N VMs as `prefix-01` … `prefix-N` — the main test-lab tool |
| `vmware_reconfigure_vm` | Change name / CPU / RAM (VM must be off) |
| `vmware_delete_vm` | Delete a VM (`confirm=true` required) |
| `vmware_screenshot` | PNG of the VM display |
| `vmware_wait_for_guest` | Wait until Tools is up (and optionally an IP) |
| `vmware_run_command` | Run a program in the guest; returns exit code + stdout/stderr |
| `vmware_run_script` | Upload and run a short script (PowerShell/cmd/bash) |
| `vmware_copy_to_guest` | Copy a file from your PC into the VM |
| `vmware_copy_from_guest` | Copy a file out of the VM |
| `vmware_list_guest_directory` | List a folder inside the guest |

**Permission modes**

| Mode | Allows |
| --- | --- |
| `read-only` (default) | List and inspect only |
| `write` | Power, create snapshots, clone, reconfigure, guest commands, screenshots |
| `destructive` | Also revert/delete snapshots and delete VMs |

For day-to-day test labs use `write`. Switch to `destructive` when you want the AI to clean up.

## Referring to VMs

Any of these work as the `vm` argument:

- Display name: `win11-golden`
- Full `.vmx` path
- Folder / stem name
- BIOS UUID from the `.vmx`

If two VMs share a name, the tool lists both paths instead of guessing.

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `VMWARE_VM_DIRS` | platform default | Folders to scan for `.vmx` files |
| `VMWARE_VMRUN_PATH` | auto | Full path to `vmrun` if not on PATH |
| `VMWARE_PRODUCT` | `ws` (Fusion on Mac) | `ws`, `fusion`, or `player` |
| `VMWARE_GUEST_USERNAME` | — | Guest OS user for run/copy tools |
| `VMWARE_GUEST_PASSWORD` | — | Guest OS password |
| `VMWARE_PERMISSION_MODE` | `read-only` | `read-only` / `write` / `destructive` |
| `VMWARE_COMMAND_TIMEOUT` | `120` | Seconds before a `vmrun` call is killed |
| `VMWARE_GUEST_TIMEOUT` | `300` | Timeout for guest programs / file copies |
| `VMWARE_BOOT_TIMEOUT` | `300` | How long to wait for Tools / IP after power on |
| `VMWARE_LOG_LEVEL` | `INFO` | Logs go to stderr |

Useful flags: `vmware-mcp --check`, `--vm-dir`, `--vmrun`, `--product`, `--guest-user`,
`--permission-mode`, `--help`.

## Tips

- **Linked clones** (`clone_type=linked`) are fast and small. Do not delete or heavily change the
  golden image while linked clones still depend on it.
- **Guest credentials** should be a local admin on the golden image, not your daily Windows login.
- Start in `read-only` if you only want the AI to inspect VMs.
- Passwords never appear in tool output.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
mypy
```

Tests use a fake `vmrun` and real `.vmx` fixtures — no VMware install required to run them.

## License

MIT. See [LICENSE](LICENSE).
