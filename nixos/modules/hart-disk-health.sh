#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS - boot-time DISK-HEALTH snapshot probe (read-only, never-brick)  [#157]
# ════════════════════════════════════════════════════════════════════════════
#
# WHY this exists (the real-HW observability twin of hart-gpu-probe /
# hart-display-health, for storage): "is the disk that HART OS lives on actually
# healthy" is a question only the real machine can answer (a VM has no SMART /
# NVMe health surface). This probe snapshots, every boot, an HONEST per-device
# readout to /run/hart/disk-health so an operator / the Disk Utility UI can read,
# off the journal or that file, which physical drives are present and whether
# SMART says each one passed. The live half (the rich per-attribute SMART/NVMe
# data) is the /api/shell/storage/health route; this is the boot snapshot.
#
# WHAT it writes (one key=value line each, machine-parseable, no JSON in sh):
#   ok=1                       <- the probe enumerated devices (0 = lsblk missing)
#   dev0.name=sda              <- short kernel name
#   dev0.path=/dev/sda         <- full device path
#   dev0.size=500107862016     <- bytes (lsblk -b)
#   dev0.rota=1                <- 1 spinning, 0 SSD/flash
#   dev0.model=Samsung SSD 860 <- model string (may contain spaces)
#   dev0.smart=passed          <- passed | failed | unknown (SMART overall health)
#   dev1.name=nvme0n1 ...
# Each device is also echoed to the journal:
#   [hart-disk-health] /dev/sda smart=passed model=Samsung SSD 860
#
# DEGRADE-NOT-DIE (the never-brick contract, same as hart-storage-fsprobe.sh):
#   `set -u` only (NOT -e): a failing probe must fall through, never abort. Every
#   external call is `timeout`-bounded + guarded and the script ALWAYS exits 0.
#   It MOUNTS nothing, WRITES nothing to any disk, LOADS no module - it only reads
#   lsblk + smartctl/nvme health and writes the one status file it is handed. No
#   lsblk -> honest ok=0, still exit 0. A drive with no SMART -> smart=unknown,
#   never a crash. It is ordered AFTER greetd (never `before greetd`), so it can
#   never delay first paint, and a slow spinning-disk SMART read is bounded per
#   device so it can never wedge the boot.
#
# Standalone (not inlined in the .nix) so a portable behavioural unit test can run
# the REAL script against stub `lsblk`/`smartctl` binaries on ANY POSIX host
# (tests/unit/test_hart_disk_health.py) WITHOUT a real disk, and so the probe
# logic lives in ONE place the module ships verbatim (the DRY gate) - the same
# pattern as hart-display-health.sh + hart-storage-fsprobe.sh.

set -u

# ── Paths + budgets (env-overridable so the unit test runs THIS script) ──
STATUS="${HART_DISK_HEALTH_FILE:-/run/hart/disk-health}"
# Per-device SMART read budget. A spinning disk that is asleep can take seconds to
# answer SMART; bound it so one slow drive can never stall the snapshot.
TIMEOUT="${HART_DISK_HEALTH_TIMEOUT:-8}"

have() { command -v "$1" >/dev/null 2>&1 ; }

# Fail-safe: always (re)create the status dir + truncate the file, guarded.
_dir=$(dirname "$STATUS")
mkdir -p "$_dir" 2>/dev/null || true
: > "$STATUS" 2>/dev/null || true

# No lsblk -> we cannot enumerate; record an HONEST ok=0 and exit clean.
if ! have lsblk; then
  printf 'ok=0\nreason=lsblk-missing\n' >> "$STATUS" 2>/dev/null || true
  echo "[hart-disk-health] lsblk missing - cannot enumerate, degrade (exit 0)" >&2
  exit 0
fi

printf 'ok=1\n' >> "$STATUS" 2>/dev/null || true

# Enumerate WHOLE disks only (-d, no partitions), as shell-safe KEY="value" pairs
# (-P), no header (-n), sizes in bytes (-b). lsblk quotes the values so the
# eval-parse below is the documented lsblk idiom.
DEVS=$(lsblk -dn -b -P -o NAME,TYPE,SIZE,ROTA,MODEL 2>/dev/null || true)

# The loop body runs in a subshell (pipe) - that is fine: every write targets the
# status FILE, and the per-device index is only used inside this same subshell.
printf '%s\n' "$DEVS" | {
  idx=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    NAME=""; TYPE=""; SIZE=""; ROTA=""; MODEL=""
    # lsblk -P emits already-quoted, shell-safe pairs (NAME="sda" TYPE="disk" ...).
    eval "$line" 2>/dev/null || continue
    [ "$TYPE" = "disk" ] || continue
    [ -n "$NAME" ] || continue
    dev="/dev/$NAME"

    smart="unknown"
    if have smartctl; then
      # -H is the overall-health self-assessment only (fast, read-only). Bounded.
      H=$(timeout "$TIMEOUT" smartctl -H "$dev" 2>/dev/null || true)
      case "$H" in
        *PASSED*|*"test result: OK"*|*"Health Status: OK"*) smart="passed" ;;
        *FAILED*|*FAILING*)                                  smart="failed" ;;
      esac
    elif have nvme; then
      # NVMe health via the spec critical-warning byte: 0 == healthy.
      W=$(timeout "$TIMEOUT" nvme smart-log "$dev" 2>/dev/null \
            | grep -iE 'critical_warning' | grep -oE '0x[0-9a-fA-F]+|[0-9]+' | head -1 || true)
      case "$W" in
        0|0x0|0x00) smart="passed" ;;
        "")         smart="unknown" ;;
        *)          smart="failed" ;;
      esac
    fi

    printf 'dev%s.name=%s\n' "$idx" "$NAME" >> "$STATUS" 2>/dev/null || true
    printf 'dev%s.path=%s\n' "$idx" "$dev" >> "$STATUS" 2>/dev/null || true
    printf 'dev%s.size=%s\n' "$idx" "$SIZE" >> "$STATUS" 2>/dev/null || true
    printf 'dev%s.rota=%s\n' "$idx" "$ROTA" >> "$STATUS" 2>/dev/null || true
    printf 'dev%s.model=%s\n' "$idx" "$MODEL" >> "$STATUS" 2>/dev/null || true
    printf 'dev%s.smart=%s\n' "$idx" "$smart" >> "$STATUS" 2>/dev/null || true
    echo "[hart-disk-health] $dev smart=$smart model=${MODEL:-unknown}" >&2

    idx=$((idx + 1))
  done
}

exit 0
