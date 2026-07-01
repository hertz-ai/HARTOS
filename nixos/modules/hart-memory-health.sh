#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS - boot-time MEMORY-HEALTH snapshot probe (read-only, never-brick) [#157]
# ════════════════════════════════════════════════════════════════════════════
#
# WHY this exists (the memory twin of hart-disk-health.sh): "did zram come up, is
# there swap headroom, is the OOM protector live" is a fact an operator / the
# memory surface should be able to read after a real boot. This probe snapshots,
# every boot, an HONEST memory readout to /run/hart/memory-health. The live half
# (refreshing totals on demand) is the /api/shell/memory route; this is the boot
# snapshot for the journal + a fast cache.
#
# WHAT it writes (one key=value line each, machine-parseable, no JSON in sh):
#   ok=1                  <- the probe read /proc/meminfo
#   mem_total_kb=16307200
#   mem_available_kb=10231044
#   swap_total_kb=8388604
#   swap_free_kb=8388604
#   zram_present=1        <- a /dev/zram* swap device exists (1/0)
#   zram_algorithm=zstd   <- the compression algo (best-effort; "" if unknown)
#   oomd_active=1         <- systemd-oomd is running (1/0/unknown)
# Also echoed to the journal:
#   [hart-memory-health] total=16307200kB avail=10231044kB zram=1 oomd=1
#
# DEGRADE-NOT-DIE (the never-brick contract): `set -u` only (NOT -e); every read
# is guarded; the script ALWAYS exits 0; it WRITES nothing except the status file
# it is handed and CHANGES no kernel state. No /proc/meminfo -> honest ok=0, still
# exit 0. Ordered AFTER greetd, so it can never delay first paint.
#
# Standalone so tests/unit/test_hart_memory_health.py runs the REAL script against
# fixture /proc files + stub binaries on any POSIX host (the DRY gate) - the same
# pattern as hart-disk-health.sh / hart-display-health.sh.

set -u

# ── Paths (env-overridable so the unit test runs THIS script against fixtures) ──
STATUS="${HART_MEMORY_HEALTH_FILE:-/run/hart/memory-health}"
MEMINFO="${HART_MEMINFO_FILE:-/proc/meminfo}"
# Where /dev/zram* device nodes live (overridable for the test's fake tree).
ZRAM_GLOB="${HART_ZRAM_GLOB:-/dev/zram}"

have() { command -v "$1" >/dev/null 2>&1 ; }

_dir=$(dirname "$STATUS")
mkdir -p "$_dir" 2>/dev/null || true
: > "$STATUS" 2>/dev/null || true

if [ ! -r "$MEMINFO" ]; then
  printf 'ok=0\nreason=meminfo-unreadable\n' >> "$STATUS" 2>/dev/null || true
  echo "[hart-memory-health] $MEMINFO unreadable - degrade (exit 0)" >&2
  exit 0
fi

# Pull the four meminfo fields we report. grep+awk are read-only; guarded.
_field() {
  grep -iE "^$1:" "$MEMINFO" 2>/dev/null | awk '{print $2}' | head -1 || true
}
MEM_TOTAL=$(_field "MemTotal")
MEM_AVAIL=$(_field "MemAvailable")
SWAP_TOTAL=$(_field "SwapTotal")
SWAP_FREE=$(_field "SwapFree")

# zram presence: a /dev/zram0 (or the fake glob) node exists.
ZRAM_PRESENT=0
for _z in "${ZRAM_GLOB}"*; do
  [ -e "$_z" ] || continue
  ZRAM_PRESENT=1
  break
done

# zram algorithm (best-effort): zramctl reports the ALGORITHM column. Missing tool
# or no device -> honest empty string, never a failure.
ZRAM_ALGO=""
if [ "$ZRAM_PRESENT" = "1" ] && have zramctl; then
  ZRAM_ALGO=$(zramctl --noheadings --output ALGORITHM 2>/dev/null | awk 'NF{print $1; exit}' || true)
fi

# systemd-oomd liveness (best-effort): unknown if systemctl is absent.
OOMD_ACTIVE="unknown"
if have systemctl; then
  if systemctl is-active systemd-oomd >/dev/null 2>&1; then
    OOMD_ACTIVE=1
  else
    OOMD_ACTIVE=0
  fi
fi

{
  printf 'ok=1\n'
  printf 'mem_total_kb=%s\n' "${MEM_TOTAL:-0}"
  printf 'mem_available_kb=%s\n' "${MEM_AVAIL:-0}"
  printf 'swap_total_kb=%s\n' "${SWAP_TOTAL:-0}"
  printf 'swap_free_kb=%s\n' "${SWAP_FREE:-0}"
  printf 'zram_present=%s\n' "$ZRAM_PRESENT"
  printf 'zram_algorithm=%s\n' "$ZRAM_ALGO"
  printf 'oomd_active=%s\n' "$OOMD_ACTIVE"
} >> "$STATUS" 2>/dev/null || true

echo "[hart-memory-health] total=${MEM_TOTAL:-0}kB avail=${MEM_AVAIL:-0}kB zram=$ZRAM_PRESENT oomd=$OOMD_ACTIVE" >&2
exit 0
