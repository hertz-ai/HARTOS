#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS — post-boot DISPLAY-HEALTH snapshot (the never-black real-HW probe)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   The display tier ladder (hart-comp -> sway -> cage) + the paint/input
#   watchdogs are PROVEN in CI nixosTests, but three never-black holes can only
#   be OBSERVED on real hardware (a QEMU VM has no real DRM scanout / seat):
#     * #131 the first-SCANOUT marker is unbuilt, so a "black-but-healthy" Tier-1
#       (the compositor scans out a black frame yet the WebKit-load marker fires)
#       cannot be distinguished from a genuinely-painting tier in a VM.
#     * #134 the input-alive marker is not written by the real compositor, so the
#       "pointer frozen at 0,0, nothing types" failure is invisible in a VM.
#     * the DRM-master EBUSY handoff grace is real-HW only (the VM has no master).
#   This probe turns "did the screen actually come up, and on which rung" into a
#   FACT an operator / the UI can read after a real boot, with HONEST `unknown`s
#   for the markers that are not built yet (it NEVER fakes `confirmed` / `alive`).
#
#   /run/hart/display-health holds one `key=value` line per dimension, e.g.
#     tier=hart-comp     <- the latched session tier (the rung that won)
#     gpu=software       <- the hart-gpu-probe verdict (hardware|software|unknown)
#     painted=yes        <- the shell-ready first-paint marker is present
#     input=unknown      <- the input-alive marker (#134) is not written yet
#     scanout=unknown    <- the first-scanout marker (#131) is not built yet
#     screen=alive       <- derived: alive only on a CONFIRMED first paint
#   Each line is echoed to the journal too:
#     [hart-display-health] tier = hart-comp
#   so `journalctl -b -u hart-display-health` shows the verdict on a real boot.
#
# HONEST SCOPE — a SNAPSHOT, not a claim:
#   It reports the marker STATE at probe time. Absence of the input/scanout marker
#   is reported `unknown`, NEVER `dead` / `black` — absence is ambiguous (the
#   compositor build may simply not write it yet) and the in-session watchdogs are
#   the only authority that may DROP on it. `painted=no` is honest too: the floor
#   still paints by its own contract, so `screen` reads `unknown` (never `black`)
#   when the marker is merely not present yet.
#
# NEVER-BLOCK-THE-DESKTOP + FAIL-SAFE (the never-fail contract):
#   * Runs in PARALLEL with the desktop (wantedBy multi-user.target, ordered AFTER
#     greetd — NOT `before greetd`), so it can never delay first paint.
#   * `set -u` (NOT -e) + every read is `|| true`-guarded: a missing/unreadable
#     marker records its fail-safe value and the script CONTINUES; the unit ALWAYS
#     `exit 0`s (oneshot + RemainAfterExit + bounded TimeoutStartSec) so it can
#     never block or fail the boot.
#   * Every path + the optional first-paint wait budget are ENV-OVERRIDABLE so the
#     SAME script is exercised by tests/unit/test_hart_display_health.py on the dev
#     box (it points the paths at fixtures) and shipped verbatim by the module —
#     one source of truth, no parallel copy.

set -u

# ── Paths (env-overridable so the unit test runs THIS script against fixtures) ──
STATUS="${HART_DISPLAY_HEALTH_FILE:-/run/hart/display-health}"
LATCH="${HART_LATCH_FILE:-/var/lib/hart/session-tier}"
# The ACTUALLY-RUNNING tier the supervisor publishes at launch (one writer). The
# LATCH is only written on a downward DROP, so a clean hart-comp start leaves it
# absent — reading the latch then defaulted to 'cage' and misreported a fully
# working Tier-1 as cage (real-HW 2026-07-12). Prefer this live marker.
CURRENT_TIER="${HART_CURRENT_TIER_FILE:-/run/hart/session/current-tier}"
READY="${HART_SHELL_READY_FLAG:-/run/hart/session/shell-ready}"
INPUT="${HART_INPUT_ALIVE_FLAG:-/run/hart/session/input-alive}"
SCANOUT="${HART_FIRST_SCANOUT_FLAG:-/run/hart/session/first-scanout}"
GPU="${HART_GPU_RENDER_FILE:-/run/hart/gpu-render}"
# Best-effort bounded wait (seconds) for the first-paint marker before the
# snapshot, so a slow-but-fine cold boot is not always caught mid-paint. Bounded
# (and the unit caps it with TimeoutStartSec) so it can NEVER block the boot; the
# desktop already painted independently while this polls. 0 = snapshot now (the
# unit test sets 0 for determinism).
WAIT="${HART_DISPLAY_HEALTH_WAIT:-20}"

mkdir -p "$(dirname "$STATUS")" 2>/dev/null || true
# Fresh measurement every boot — never appended to a stale file.
: > "$STATUS" 2>/dev/null || true

# record <key> <value> — one honest key=value line + a journal announcement.
record() {
  printf '%s=%s\n' "$1" "$2" >> "$STATUS" 2>/dev/null || true
  echo "[hart-display-health] $1 = $2" >&2
}

# ── Best-effort bounded wait for first paint (never blocks beyond WAIT) ──
waited=0
while [ "$WAIT" -gt 0 ] 2>/dev/null && [ "$waited" -lt "$WAIT" ]; do
  [ -e "$READY" ] && break
  sleep 1
  waited=$((waited + 1))
done

# ── tier: the tier that is actually RUNNING now. Prefer the supervisor's live
#    `current-tier` marker (written at each launch); fall back to the drop-LATCH
#    (only written on a downward drop), then to the cage FLOOR. This is the fix for
#    the weeks-long misreport where a clean hart-comp start left the latch absent so
#    this defaulted to 'cage' and hid a working Tier-1. Order: live > latch > floor. ──
tier=cage
if [ -r "$CURRENT_TIER" ]; then
  _t=$(cat "$CURRENT_TIER" 2>/dev/null | tr -d '[:space:]')
  case "$_t" in
    hart-comp|sway|cage) tier="$_t" ;;
  esac
elif [ -r "$LATCH" ]; then
  _t=$(cat "$LATCH" 2>/dev/null | tr -d '[:space:]')
  case "$_t" in
    hart-comp|sway|cage) tier="$_t" ;;
  esac
fi
record tier "$tier"

# ── gpu: the hart-gpu-probe verdict. `unknown` if the probe has not written it
#    (it runs BEFORE greetd, so on a normal boot it is present). ──
gpu=unknown
if [ -r "$GPU" ]; then
  _g=$(cat "$GPU" 2>/dev/null | tr -d '[:space:]')
  case "$_g" in
    hardware|software) gpu="$_g" ;;
  esac
fi
record gpu "$gpu"

# ── painted: the shell-ready first-paint marker (the paint-watchdog's signal). ──
if [ -e "$READY" ]; then painted=yes; else painted=no; fi
record painted "$painted"

# ── input (#134): present => the compositor proved its input pipeline live;
#    ABSENT => `unknown` (the marker may not be built yet — absence is NOT proof
#    of input-death from a post-boot snapshot; only the in-session input watchdog
#    may DROP on it). NEVER report `dead` here. ──
if [ -e "$INPUT" ]; then input=live; else input=unknown; fi
record input "$input"

# ── scanout (#131): present => a real (non-black) frame was confirmed scanned out;
#    ABSENT => `unknown` (the first-scanout marker is unbuilt — honest unknown,
#    never `black`). This is the field that, once the compositor writes the marker,
#    distinguishes a black-but-healthy Tier-1 from a genuinely painting one. ──
if [ -e "$SCANOUT" ]; then scanout=confirmed; else scanout=unknown; fi
record scanout "$scanout"

# ── screen: the derived summary. `alive` ONLY on a confirmed first paint; else
#    `unknown` (never `black` — the floor still paints by its own contract, so a
#    merely-absent marker at snapshot time is not proof of a black screen). ──
if [ "$painted" = "yes" ]; then screen=alive; else screen=unknown; fi
record screen "$screen"

# Always succeed — this is a measurement, never a gate.
exit 0
