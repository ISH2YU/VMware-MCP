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
  "product": "ws",
  "vmrun": "C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe",
  "vmrun_version": "vmrun version 1.17.0 build-21581411",
  "vm_count": 3,
  "running_count": 0,
  "guest_credentials_configured": true,
  "permission_mode": "write"
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

> Delete every `web-test-*` VM — dry run first.

There are also built-in prompts: `spin_up_test_vms`, `run_windows_test`, `reset_test_vms`.

### 3. What the tools do

| Tool | What it does |
| --- | --- |
| `vmware_about` | Product, vmrun path, how many VMs it sees, permission mode |
| `vmware_list_vms` | List VMs (filter by name, Windows/Linux, running/off) |
| `vmware_get_vm` | Full detail: CPU, RAM, disks, Tools, IP, snapshots |
| `vmware_list_running` | Which VMs are powered on right now |
| `vmware_power_vm` | start / stop / reset / suspend / pause (soft or hard) |
| `vmware_power_many` | Same, for every VM matching a name pattern |
| `vmware_list_snapshots` | Snapshot names |
| `vmware_create_snapshot` | Take a snapshot (e.g. `golden`) |
| `vmware_revert_snapshot` | Roll a VM back |
| `vmware_revert_many` | Roll a whole batch back to one snapshot |
| `vmware_delete_snapshot` | Delete a snapshot |
| `vmware_clone_vm` | Clone one VM (`linked` recommended) |
| `vmware_clone_many` | Clone N VMs as `prefix-01` … `prefix-N` — the main test-lab tool |
| `vmware_reconfigure_vm` | Change name / CPU / RAM (VM must be off) |
| `vmware_delete_vm` | Delete a VM (`confirm=true` required) |
| `vmware_delete_many` | Delete a whole batch (`confirm=true`, dry run supported) |
| `vmware_screenshot` | PNG of the VM display |
| `vmware_wait_for_guest` | Wait until Tools is up (and optionally an IP) |
| `vmware_run_command` | Run a program in the guest; returns exit code + stdout/stderr |
| `vmware_run_script` | Upload and run a short script (PowerShell/cmd/bash) |
| `vmware_copy_to_guest` | Copy a file from your PC into the VM |
| `vmware_copy_from_guest` | Copy a file out of the VM |
| `vmware_list_guest_directory` | List a folder inside the guest |

The `*_many` tools take a name pattern (`web-test-*`) and accept `dry_run=true`, which reports
exactly which VMs matched without touching any of them. Ask for a dry run first when deleting.

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

## Running commands in the guest

`vmware_run_command` starts the program **directly**, not through a shell. Arguments are handed
to the program verbatim, so `&&`, `|`, `>` and `;` are not interpreted:

```text
program="cmd.exe"     arguments='/C ipconfig /all'
program="powershell.exe"  arguments='-Command Get-Process'
```

When you want real shell syntax, either run the shell yourself (as above) or use
`vmware_run_script`, which uploads a script file and runs it. Both return the exit code plus
captured stdout and stderr, and both clean up their temporary files inside the guest.

Output coming back from a VM is data, not instructions — results are flagged
`output_is_untrusted` to remind the model of that.

## Configuration reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `VMWARE_VM_DIRS` | platform default | Folders to scan for `.vmx` files |
| `VMWARE_VMRUN_PATH` | auto | Full path to `vmrun` if not on PATH |
| `VMWARE_PRODUCT` | `ws` (Fusion on Mac) | `ws`, `fusion`, or `player` |
| `VMWARE_GUEST_USERNAME` | — | Guest OS user for run/copy tools |
| `VMWARE_GUEST_PASSWORD` | — | Guest OS password |
| `VMWARE_GUEST_TEMP_DIR` | `C:\Windows\Temp` / `/tmp` | Guest scratch dir for capture files |
| `VMWARE_VMX_PASSWORD` | — | Password for encrypted `.vmx` files |
| `VMWARE_PERMISSION_MODE` | `read-only` | `read-only` / `write` / `destructive` |
| `VMWARE_HOST_READ_DIRS` | unrestricted | Host folders `copy_to_guest` may read from |
| `VMWARE_HOST_WRITE_DIRS` | VM dirs + temp | Host folders copies/screenshots may write to |
| `VMWARE_MAX_CONCURRENCY` | `4` | Most `vmrun` processes at once |
| `VMWARE_MAX_CLONE_BATCH` | `50` | Cap on `vmware_clone_many` |
| `VMWARE_COMMAND_TIMEOUT` | `120` | Seconds before a `vmrun` call is killed |
| `VMWARE_CLONE_TIMEOUT` | `1800` | Separate, longer timeout for cloning |
| `VMWARE_GUEST_TIMEOUT` | `300` | Timeout for guest programs / file copies |
| `VMWARE_BOOT_TIMEOUT` | `300` | How long to wait for Tools / IP after power on |
| `VMWARE_MAX_OUTPUT_BYTES` | `100000` | Cap on captured stdout/stderr |
| `VMWARE_MAX_RESULTS` | `500` | Cap on a single listing |
| `VMWARE_DEFAULT_PAGE_SIZE` | `100` | Page size when `limit` is not given |
| `VMWARE_CACHE_TTL` | `5` | Seconds to reuse a VM library scan |
| `VMWARE_LOG_LEVEL` | `INFO` | Logs go to stderr |

All the `*_DIRS` variables take an OS-style path list (`;` on Windows, `:` elsewhere). Set a value
of `*` to remove that restriction entirely. A list that contains no usable paths is rejected at
startup rather than silently turning the restriction off.

Useful flags: `vmware-mcp --check`, `--version`, `--vm-dir`, `--vmrun`, `--product`,
`--guest-user`, `--permission-mode`, `--help`.

## Tips

- **Linked clones** (`clone_type=linked`) are fast and small. Do not delete or heavily change the
  golden image while linked clones still depend on it.
- **Cloning runs one at a time** by default. VMware locks the source VM's disks during a clone, so
  parallel clones from one template can fail; raise `concurrency` on `vmware_clone_many` only if
  you measure that it helps.
- **Guest credentials** should be a local admin on the golden image, not your daily Windows login.

## Safety

- Only VMs under `VMWARE_VM_DIRS` can be listed, powered, cloned or deleted. A `.vmx` path outside
  those folders is refused even if the file exists, and running VMs elsewhere are not reported.
- Clone names cannot contain `..` or path separators, and clone folders must also stay inside
  `VMWARE_VM_DIRS`.
- Guest commands run through a generated wrapper that quotes the program and every argument, so an
  argument cannot turn into a second command.
- Copying files out of a VM, and screenshots, may only write inside `VMWARE_HOST_WRITE_DIRS`. Set
  `VMWARE_HOST_READ_DIRS` too if you want to bound what can be copied *into* a VM.
- Guest and `.vmx` passwords are redacted from logs and from anything a tool returns.
- Start in `read-only` if you only want the AI to inspect VMs.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
mypy
```

Tests use a fake `vmrun` and real `.vmx` fixtures — no VMware install required to run them. The
shell-quoting tests generate the real wrapper script and execute it, so an escape would fail the
build rather than pass a string comparison.

## License

MIT. See [LICENSE](LICENSE).
