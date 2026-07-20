#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS - cross-OS filesystem real-HW driver probe (#145 interop)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY this exists (the real-HW half the VM cannot reach):
#   tests/storage-filesystems.nix PROVES, in a VM, that a fresh kernel can mkfs +
#   mount + read/write NTFS/exFAT/FAT/ext4/btrfs. But "did the #145 interop config
#   actually deliver the drivers on THIS physical machine's kernel" is a question
#   only real hardware can answer (a stripped/odd kernel could lack ntfs3 or exfat
#   even when the VM kernel had them). This script is that real-HW READOUT: for
#   each cross-OS filesystem it reports `ok` (the driver is available to the
#   running kernel) or `missing`, WITHOUT mounting anything and WITHOUT loading a
#   module - so it is safe to run unprivileged on every boot.
#
# WHO calls it:
#   * hart-compat-smoketest.nix (every boot, in PARALLEL with the desktop) -> the
#     status-file mode appends one honest `fs_<name>=ok|missing` line per
#     filesystem to /run/hart/compat-status (+ a journal line), so an operator who
#     plugs a Windows/macOS-formatted disk on real iron can read, off the journal,
#     whether the kernel can mount it.
#   * hart-sandbox.nix (`hart sandbox test-windows`) -> the --query mode returns a
#     single `ok`/`missing` per filesystem for the PASS/SKIP validation readout.
#   Both reach it via PATH (hart-storage.nix installs it when hart.storage.enable),
#   each guarded by `command -v` so a build without the interop set is a clean skip.
#
# HOW it decides "available" (read-only, never loads, never needs root):
#   1. The filesystem is already registered in the running kernel
#      ($HART_PROC_FILESYSTEMS, default /proc/filesystems = loaded or built-in).
#   2. Else its module is present in /lib/modules for this kernel, via `modinfo`
#      (read-only, non-root, resolves both .ko modules AND built-ins) - this does
#      NOT load the module, so it can never hang or fault the boot.
#   3. NTFS is ALSO satisfied by the ntfs-3g userspace FUSE mount helper
#      (`mount.ntfs`) even if the in-kernel ntfs3 driver is absent.
#
# DEGRADE-NOT-DIE (the never-brick contract, same as hart-audio-unmute.sh):
#   `set -u` only (NOT -e): a failing probe must fall through, never abort. Every
#   external call is guarded and the script ALWAYS exits 0. No modinfo / no
#   /proc/filesystems / no helper -> the filesystem is honestly recorded `missing`,
#   the script still exits 0. It mounts nothing, loads nothing, writes only the
#   status file it is handed - it can never wedge, brick, or fail a boot.
#
# Standalone (not inlined in the .nix) so a portable behavioural unit test can run
# the REAL script against a stub `modinfo` + a fake /proc/filesystems on ANY POSIX
# host (tests/unit/test_hart_storage_fsprobe.py) WITHOUT a Linux VM, and so the
# probe logic lives in ONE place that every caller shares (the DRY gate).

set -u

# Allow the kernel-filesystems source to be overridden so the unit test can point
# at a fake table; defaults to the real one on a live system.
PROCFS="${HART_PROC_FILESYSTEMS:-/proc/filesystems}"

have() { command -v "$1" >/dev/null 2>&1 ; }

# fs_available <fsname> -> exit 0 if the running kernel can mount that filesystem.
# Read-only: it inspects /proc/filesystems and `modinfo`, and (for ntfs) the FUSE
# mount helper. It NEVER loads a module, so it needs no privilege and cannot hang.
fs_available() {
  _fs="$1"
  # The kernel MODULE name can differ from the filesystem name. ntfs is served by
  # the in-kernel ntfs3 RW driver (or the legacy `ntfs` module); every other
  # entry in the #145 set matches its own name.
  case "$_fs" in
    ntfs) _mods="ntfs3 ntfs" ;;
    *)    _mods="$_fs" ;;
  esac

  # 1. Already registered in the running kernel (loaded module OR built-in).
  for _m in $_mods; do
    if grep -qw "$_m" "$PROCFS" 2>/dev/null; then
      return 0
    fi
  done

  # 2. Driver present for this kernel (module .ko OR built-in), via read-only,
  #    non-root `modinfo` - this does NOT load anything.
  if have modinfo; then
    for _m in $_mods; do
      if modinfo "$_m" >/dev/null 2>&1; then
        return 0
      fi
    done
  fi

  # 3. NTFS is also fully read/write via the ntfs-3g userspace FUSE helper.
  if [ "$_fs" = ntfs ] && have mount.ntfs; then
    return 0
  fi

  return 1
}

# verdict <fsname> -> echoes "ok" or "missing" (never empty).
verdict() {
  if fs_available "$1"; then
    echo ok
  else
    echo missing
  fi
}

# ── Mode A: single-filesystem query (hart-sandbox) ──────────────────────────
#   hart-storage-fsprobe --query <fs>   ->  prints ok|missing, exit 0
if [ "${1:-}" = "--query" ]; then
  _q="${2:-}"
  if [ -n "$_q" ]; then
    verdict "$_q"
  else
    echo missing
  fi
  exit 0
fi

# ── Mode B: status-file write for a list of filesystems (compat-smoketest) ──
#   hart-storage-fsprobe <status_file> <fs...>  -> append fs_<name>=verdict lines
STATUS="${1:-}"
if [ -z "$STATUS" ]; then
  # Nothing to write to and not a query -> honest no-op (degrade), exit 0.
  exit 0
fi
shift

for _fs in "$@"; do
  _v=$(verdict "$_fs")
  printf 'fs_%s=%s\n' "$_fs" "$_v" >> "$STATUS" 2>/dev/null || true
  echo "[hart-storage-fsprobe] fs_$_fs = $_v" >&2
done

exit 0
