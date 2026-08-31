# Bugbot rules for VMware MCP

This repository is a **local** VMware Workstation / Fusion / Player MCP server.
It drives VMs through `vmrun`. There is no vSphere, vCenter, ESXi or pyVmomi.

## Must-flag

- Operating on a `.vmx` (power, clone, snapshot, delete, guest ops) that is
  **outside** `VMWARE_VM_DIRS`. Path lookup is a sandbox, not a convenience.
- Clone destinations that escape the configured VM directories, including names
  with `..` or path separators.
- Guest command wrappers that interpolate `program` / `arguments` into `cmd.exe`
  or `/bin/sh -c` without quoting. Wrappers must use argv, `shlex.quote`, or a
  generated script whose literals are escaped.
- `wait_for_guest` using two independent timeouts (Tools, then IP) so the call
  can run for up to twice `timeout_seconds`.
- Mutating tools that do not call `settings.require(PermissionMode.WRITE)` (or
  `DESTRUCTIVE` for revert/delete). `vmware_delete_vm` without `confirm=true`.
- Logging or returning guest / `.vmx` passwords. `Settings.describe()` must stay
  redacted. `-gp` / `-vp` in logs must be `***`.
- `vmrun` subprocesses with no timeout.

## Do not flag

- `"backend": "workstation"` in JSON — that is product metadata, not a leftover
  remote-hypervisor abstraction.
- Tests that use `FakeVmrun` instead of a real VMware install.
- Linked clones that keep depending on a golden snapshot.
- Empty guest passwords: some lab images have a blank password on purpose.
