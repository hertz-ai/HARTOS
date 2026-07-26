#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M2 — STAGE B: prove the wlr-layer-shell with the SIMPLE swaybg client.
# ════════════════════════════════════════════════════════════════════════════
# swaybg is the canonical minimal BACKGROUND layer-shell client: it creates a
# zwlr_layer_shell_v1 surface in the BACKGROUND layer, anchored to all 4 edges,
# exclusive-zone 0 — the EXACT role the glass shell uses. If swaybg's background
# fills hart-comp's output, the layer-shell map + initial-configure + render +
# frame-callback path is end-to-end correct.
#
# Run as root; drops to sathish. Points swaybg at hart-comp's OWN socket (the
# arg), NOT the host.
set -u
HART_SOCK="${1:-wayland-2}"
SWAYBG_LOG=/tmp/m2-swaybg.log
HART_LOG=/tmp/m2-hartcomp.log

# Clear hart-comp's log marker region so we can see NEW layer-map lines from this
# swaybg run (we just note the current line count).
PRE_LINES=$(wc -l <"$HART_LOG" 2>/dev/null || echo 0)

# `pkill -x swaybg` matches the PROCESS NAME exactly — NOT this script's argv
# (which contains "swaybg" and would be killed by `pkill -f swaybg`).
pkill -x swaybg 2>/dev/null || true
sleep 0.3
rm -f "$SWAYBG_LOG"

# Stage the inner launcher (avoids the '#rrggbb' color being eaten by shell nesting)
# into a sathish-readable path.
cp /root/hart-comp/scripts/m2_run_swaybg.sh /tmp/m2_run_swaybg.sh 2>/dev/null || \
  cp /mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/scripts/m2_run_swaybg.sh /tmp/m2_run_swaybg.sh
chmod 755 /tmp/m2_run_swaybg.sh
chown sathish:sathish /tmp/m2_run_swaybg.sh

echo "[m2_swaybg] attaching swaybg (solid #1a6ef5) to hart-comp socket $HART_SOCK"
# The inner launcher opens $SWAYBG_LOG itself (sathish owns the fd — root can't
# redirect into a sathish-owned /tmp file on this WSL mount).
setsid runuser -u sathish -- bash -c "exec /tmp/m2_run_swaybg.sh '$HART_SOCK' > '$SWAYBG_LOG' 2>&1" &
echo "  swaybg pid-group $!"

# Give it a few render iterations.
for i in $(seq 1 16); do
  if grep -q "Found config" "$SWAYBG_LOG" 2>/dev/null; then break; fi
  sleep 0.25
done
sleep 1

echo ""
echo "=== swaybg log ==="
cat "$SWAYBG_LOG" 2>/dev/null || echo "(empty)"

echo ""
echo "=== swaybg alive? (proves it didn't crash on a protocol error) ==="
pgrep -af "swaybg" | grep -v pgrep | head

echo ""
echo "=== hart-comp NEW log lines since swaybg attached (layer map + render) ==="
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }
tail -n +"$((PRE_LINES+1))" "$HART_LOG" 2>/dev/null | strip_ansi \
  | grep -iE "layer|render error|failed|window.opened|panic" | head -20
echo "--- (no 'render error' / 'failed to map layer' above = layer surface is composited) ---"

echo ""
echo "=== render-error scan over the WHOLE hart-comp log (must be empty) ==="
strip_ansi <"$HART_LOG" | grep -iE "render error|failed to map layer surface" | head || echo "NONE — clean"
