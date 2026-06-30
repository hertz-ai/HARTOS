#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS - boot-time audio rescue (unmute + sane default volume)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY this exists (a real-HW failure the steward hit):
#   A sound SINK existed but was MUTED (or pinned at volume 0) on boot, so the
#   desktop had "no audio out" with no obvious cause. WirePlumber persists the
#   per-user mute/volume state across reboots, so a once-muted default sink stays
#   silent forever until the user digs into a mixer. A fresh OS that boots SILENT
#   feels broken.
#
# WHAT it does (best-effort, NEVER fails the session):
#   On each graphical session start, once PipeWire/WirePlumber are up:
#     1. If there is NO default sink yet -> degrade cleanly: log + exit 0 (no
#        audio device is a valid state, not an error; boot continues).
#     2. UNMUTE the default sink (this alone fixes the muted-on-boot bug).
#     3. RESCUE the level ONLY when it currently reads 0 (or is unreadable): set
#        it to the configured floor (default 60%). A deliberate non-zero user
#        level is left untouched, so this never clobbers "I set it to 30% on
#        purpose" on every login - it only rescues a SILENT default.
#
# DEGRADE-NOT-DIE (the never-brick contract):
#   `set -u` only (NOT -e): a failing probe must fall through, never abort. Every
#   external call is guarded (`|| true`) and the script ALWAYS exits 0. Missing
#   wpctl AND pactl -> clean no-op. No default sink -> clean no-op. A wpctl/pactl
#   that errors -> clean no-op. It can never wedge, crash, or fail the session.
#
# TRANSPORT: wpctl (WirePlumber) first, pactl (PipeWire-pulse) fallback - the
#   same two-tool order the shell's read-side volume probe uses
#   (integrations/agent_engine/liquid_ui_service.py:_volume_get), so the OS has
#   ONE audio-control vocabulary, not a parallel one.
#
# Standalone (not inlined in the .nix) so a portable behavioural unit test can
# run the REAL script against stub wpctl/pactl on PATH and assert the decisions
# (tests/unit/test_hart_audio_unmute.py) WITHOUT a Linux VM. hart-audio.nix loads
# it via writeShellScript(readFile ./hart-audio-unmute.sh) and passes the
# configured volume percent as $1.

set -u

log() { echo "[hart-audio-unmute] $*" >&2 ; }

# ── Configured floor volume (percent). $1 from the unit; default 60; clamped. ──
VOL_PCT="${1:-60}"
case "$VOL_PCT" in
  ''|*[!0-9]*) VOL_PCT=60 ;;   # non-numeric -> default
esac
[ "$VOL_PCT" -gt 150 ] && VOL_PCT=150
[ "$VOL_PCT" -lt 0 ]   && VOL_PCT=0

# wpctl wants a 0.00-1.50 FRACTION; build it with pure POSIX arithmetic so we
# need no awk/bc (60 -> 0.60, 100 -> 1.00, 150 -> 1.50, 5 -> 0.05).
WHOLE=$((VOL_PCT / 100))
FRAC=$((VOL_PCT % 100))
[ "$FRAC" -lt 10 ] && FRAC="0$FRAC"
FRACTION="${WHOLE}.${FRAC}"

have() { command -v "$1" >/dev/null 2>&1 ; }

# Does a usable default sink exist on EITHER transport? (degrade probe)
have_sink() {
  if have wpctl && wpctl get-volume @DEFAULT_AUDIO_SINK@ >/dev/null 2>&1; then
    return 0
  fi
  if have pactl; then
    _s=$(pactl get-default-sink 2>/dev/null || true)
    [ -n "$_s" ] && [ "$_s" != "@DEFAULT_SINK@" ] && return 0
  fi
  return 1
}

# ── Bounded wait for the default sink. WirePlumber can enumerate a beat after
# its unit reports "started"; poll up to ~8s (16 x 0.5s) so we do not race it.
# This loop can never hang boot: it is a USER oneshot ordered after pipewire and
# the unit carries TimeoutStartSec; worst case it gives up and no-ops.
tries=0
while [ "$tries" -lt 16 ]; do
  have_sink && break
  tries=$((tries + 1))
  sleep 0.5 2>/dev/null || true
done

if ! have_sink; then
  log "no default sink after wait -> no audio device to rescue (clean no-op, boot continues)"
  exit 0
fi

# ── wpctl (WirePlumber) path - preferred ───────────────────────────────────
if have wpctl && wpctl get-volume @DEFAULT_AUDIO_SINK@ >/dev/null 2>&1; then
  VOLOUT=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null || true)  # "Volume: 0.00 [MUTED]"

  # 1. Always unmute (fixes the muted-on-boot "no audio out").
  wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 2>/dev/null || true

  # 2. Rescue the level ONLY if it currently reads 0 (do not clobber a level the
  #    user chose on purpose). Parse the leading fraction from the get-volume line.
  CUR=$(printf '%s' "$VOLOUT" | sed -n 's/^Volume:[[:space:]]*\([0-9][0-9.]*\).*/\1/p')
  case "$CUR" in
    ''|0|0.|0.0|0.00|0.000)
      wpctl set-volume @DEFAULT_AUDIO_SINK@ "$FRACTION" 2>/dev/null || true
      log "wpctl: unmuted + rescued silent default sink to ${VOL_PCT}% (${FRACTION})"
      ;;
    *)
      log "wpctl: unmuted default sink; left existing level ${CUR} (not clobbering a deliberate volume)"
      ;;
  esac
  exit 0
fi

# ── pactl (PipeWire-pulse) fallback ────────────────────────────────────────
if have pactl; then
  SINK=$(pactl get-default-sink 2>/dev/null || true)
  if [ -n "$SINK" ] && [ "$SINK" != "@DEFAULT_SINK@" ]; then
    # 1. Always unmute.
    pactl set-sink-mute "$SINK" 0 2>/dev/null || true

    # 2. Rescue only if the first reported channel percentage is 0.
    PVOLOUT=$(pactl get-sink-volume "$SINK" 2>/dev/null || true)
    PVOL=$(printf '%s' "$PVOLOUT" | grep -oE '[0-9]+%' | head -1 | tr -d '%')
    if [ -z "$PVOL" ] || [ "$PVOL" = "0" ]; then
      pactl set-sink-volume "$SINK" "${VOL_PCT}%" 2>/dev/null || true
      log "pactl: unmuted + rescued silent default sink '$SINK' to ${VOL_PCT}%"
    else
      log "pactl: unmuted default sink '$SINK'; left existing level ${PVOL}% (not clobbering)"
    fi
    exit 0
  fi
fi

log "no usable wpctl/pactl control path -> clean no-op (boot continues)"
exit 0
