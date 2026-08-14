#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# HART OS - boot-time audio rescue (unmute + first-boot 100% + hotplug reselect)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY this exists (real-HW failures the steward hit):
#   (1) A sound SINK existed but was MUTED (or pinned at volume 0) on boot, so the
#       desktop had "no audio out" with no obvious cause. WirePlumber persists the
#       per-user mute/volume state across reboots, so a once-muted default sink
#       stays silent forever until the user digs into a mixer.
#   (2) A fresh OS that boots at some tiny/zero volume feels broken - a brand-new
#       install should be AUDIBLE out of the box (steward: default sink at 100%).
#
# WHAT it does (best-effort, NEVER fails the session):
#   On each graphical session start, once PipeWire/WirePlumber are up:
#     1. If there is NO default sink yet -> try a hotplug-safe RESELECTION: if
#        sinks EXIST but none is the assigned default, promote the first one.
#        Still nothing -> degrade cleanly: log + exit 0 (no audio device is a
#        valid state, not an error; boot continues).
#     2. UNMUTE the default sink (this alone fixes the muted-on-boot bug).
#     3. FIRST BOOT (per-user, once): set the default sink to the full floor
#        (default 100%) UNCONDITIONALLY so the fresh OS is audible. The one-time
#        flag is a per-user stamp; claiming it (creating it) is what flips a run
#        from "first boot" to "subsequent".
#     4. EVERY LATER BOOT: rescue the level ONLY when it currently reads 0 (or is
#        unreadable) - set it to the floor. A deliberate non-zero user level is
#        left untouched, so this never clobbers "I set it to 30% on purpose" on
#        every login; it only rescues a SILENT default.
#
# DEGRADE-NOT-DIE (the never-brick contract):
#   `set -u` only (NOT -e): a failing probe must fall through, never abort. Every
#   external call is guarded (`|| true`) and the script ALWAYS exits 0. Missing
#   wpctl AND pactl -> clean no-op. No default sink -> clean no-op. A wpctl/pactl
#   that errors -> clean no-op. It can never wedge, crash, or fail the session.
#   If the first-boot stamp cannot be written (unwritable HOME) the claim fails
#   and we fall back to the never-clobber rescue, so a chosen level is never
#   stomped every login even in that edge.
#
# TRANSPORT: wpctl (WirePlumber) first, pactl (PipeWire-pulse) fallback - the
#   same two-tool order the shell's read-side volume probe uses
#   (integrations/agent_engine/liquid_ui_service.py:_volume_get), so the OS has
#   ONE audio-control vocabulary, not a parallel one. All control targets the
#   DYNAMIC default handle (@DEFAULT_AUDIO_SINK@ / @DEFAULT_SINK@), so it always
#   follows the CURRENT default - hotplug reselection at the control level.
#
# Standalone (not inlined in the .nix) so a portable behavioural unit test can
# run the REAL script against stub wpctl/pactl on PATH and assert the decisions
# (tests/unit/test_hart_audio_unmute.py) WITHOUT a Linux VM. hart-audio.nix loads
# it via writeShellScript(readFile ./hart-audio-unmute.sh) and passes the
# configured volume percent as $1.

set -u

log() { echo "[hart-audio-unmute] $*" >&2 ; }

# ── Configured floor volume (percent). $1 from the unit; default 100; clamped. ─
VOL_PCT="${1:-100}"
case "$VOL_PCT" in
  ''|*[!0-9]*) VOL_PCT=100 ;;   # non-numeric -> default
esac
[ "$VOL_PCT" -gt 150 ] && VOL_PCT=150
[ "$VOL_PCT" -lt 0 ]   && VOL_PCT=0

# wpctl wants a 0.00-1.50 FRACTION; build it with pure POSIX arithmetic so we
# need no awk/bc (100 -> 1.00, 60 -> 0.60, 150 -> 1.50, 5 -> 0.05).
WHOLE=$((VOL_PCT / 100))
FRAC=$((VOL_PCT % 100))
[ "$FRAC" -lt 10 ] && FRAC="0$FRAC"
FRACTION="${WHOLE}.${FRAC}"

have() { command -v "$1" >/dev/null 2>&1 ; }

# ── First-boot stamp (per-user). The full floor is set ONCE, on the first
# graphical session that actually finds a sink; every later boot only unmutes +
# rescues a silent sink. `${HOME:-/tmp}` guard keeps `set -u` from aborting when
# HOME is unset in a minimal context (degrade-not-die). ──
STAMP_DIR="${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/hart"
STAMP="$STAMP_DIR/audio-firstboot"
first_boot=0

# Claim the one-time first-boot flag. Returns 0 (this IS the first boot) only if
# the stamp did not exist AND we successfully created it - so an unwritable-HOME
# boot can never re-fire the unconditional set and clobber a user's level.
claim_first_boot() {
  [ -e "$STAMP" ] && return 1
  mkdir -p "$STAMP_DIR" 2>/dev/null || return 1
  : > "$STAMP" 2>/dev/null || return 1
  return 0
}

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

# ── Default-sink RESELECTION (hotplug-safe). If sinks EXIST but none is the
# assigned default (a hotplug edge where routing did not pick one), promote the
# first available sink so playback + the rescue have a target. pactl is the
# parseable transport (`list short sinks`); once IT sets the default, wpctl's
# @DEFAULT_AUDIO_SINK@ resolves to it too. Best-effort + guarded: it can only
# ADD a default where there was NONE, never repoint a working one. ──
reselect_default_sink() {
  have pactl || return 0
  _cur=$(pactl get-default-sink 2>/dev/null || true)
  [ -n "$_cur" ] && [ "$_cur" != "@DEFAULT_SINK@" ] && return 0   # a default already exists
  _first=$(pactl list short sinks 2>/dev/null | head -1 | cut -f2)
  if [ -n "$_first" ]; then
    pactl set-default-sink "$_first" 2>/dev/null || true
    log "reselected default sink -> $_first (hotplug: none was assigned)"
  fi
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
  # Sinks may exist with no default assigned (hotplug) -> promote one, re-check.
  reselect_default_sink
  if ! have_sink; then
    log "no default sink after wait -> no audio device to rescue (clean no-op, boot continues)"
    exit 0
  fi
  log "recovered a default sink via reselection"
fi

# We have a sink: claim the one-time first-boot flag NOW (only after a sink is
# found, so a headless boot never burns it).
claim_first_boot && first_boot=1

# ── ALSA ELEMENT unmute — the layer BENEATH PipeWire ────────────────────────
# REAL-HW GAP THIS CLOSES (2026-08-12, Samsung NP550P5C): this script only ever
# unmuted at the PipeWire/Pulse layer (wpctl/pactl, below). On that laptop the
# PipeWire sink was already healthy and UNMUTED at 40% -- while ALSA's own
# `Headphone` element was [off] underneath, so the machine was SILENT and every
# check here reported success. A rescue that cannot see the layer that is actually
# muted is a silent failure of the rescue itself: `wpctl get-volume` showed a
# perfectly good sink the whole time.
#
# PipeWire drives ALSA but does NOT un-mute individual mixer elements it did not
# mute, so a muted-at-the-ALSA-level element (saved in alsactl state, or shipped
# that way by the codec defaults) survives every wpctl/pactl unmute. Unmute the
# standard output elements directly. Best-effort by design: `amixer` may be absent,
# and a control that does not exist on this codec simply fails -- both are fine and
# must never abort the rescue (hence `|| true` and the `have` guard). We only ever
# UNMUTE here; volume levels stay the wpctl/pactl path's job, so this cannot clobber
# a deliberate level.
if have amixer; then
  for _card in 0 1 2; do
    amixer -c "$_card" scontrols >/dev/null 2>&1 || continue
    for _ctl in Master Speaker Headphone PCM Front "Bass Speaker" Desktop; do
      if amixer -c "$_card" get "$_ctl" >/dev/null 2>&1; then
        if amixer -c "$_card" get "$_ctl" 2>/dev/null | grep -q '\[off\]'; then
          amixer -c "$_card" set "$_ctl" unmute >/dev/null 2>&1 \
            && log "alsa: card${_card} '${_ctl}' was MUTED at the ALSA layer -> unmuted (PipeWire could not see this)"
        fi
      fi
    done
    # 'Auto-Mute Mode' silences the speakers whenever the codec thinks something is
    # jacked in; a false positive on a worn jack mutes the box permanently.
    if amixer -c "$_card" get "Auto-Mute Mode" >/dev/null 2>&1; then
      amixer -c "$_card" set "Auto-Mute Mode" Disabled >/dev/null 2>&1 || true
    fi
  done
fi

# ── wpctl (WirePlumber) path - preferred ───────────────────────────────────
if have wpctl && wpctl get-volume @DEFAULT_AUDIO_SINK@ >/dev/null 2>&1; then
  VOLOUT=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null || true)  # "Volume: 0.00 [MUTED]"

  # 1. Always unmute (fixes the muted-on-boot "no audio out").
  wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 2>/dev/null || true

  # 2. First boot: set the full floor UNCONDITIONALLY so a fresh OS is audible.
  if [ "$first_boot" = 1 ]; then
    wpctl set-volume @DEFAULT_AUDIO_SINK@ "$FRACTION" 2>/dev/null || true
    log "wpctl: first boot -> unmuted + set default sink to ${VOL_PCT}% (${FRACTION})"
    exit 0
  fi

  # 3. Every later boot: rescue the level ONLY if it currently reads 0 (do not
  #    clobber a level the user chose). Parse the leading fraction.
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

    # 2. First boot: set the full floor unconditionally.
    if [ "$first_boot" = 1 ]; then
      pactl set-sink-volume "$SINK" "${VOL_PCT}%" 2>/dev/null || true
      log "pactl: first boot -> unmuted + set default sink '$SINK' to ${VOL_PCT}%"
      exit 0
    fi

    # 3. Later boots: rescue only if the first reported channel percentage is 0.
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
