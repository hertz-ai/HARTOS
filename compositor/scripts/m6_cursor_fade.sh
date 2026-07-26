#!/usr/bin/env bash
# M6 cursor-MOVE + map-FADE proof, fired within the ~2.5s pre-EGL-death window.
#   • CURSOR MOVE: warp the host cursor (sway forwards absolute motion to hart-comp,
#     which repositions ITS software cursor) → DIRECT grim shows the arrow at the new
#     spot. Two warps = two arrow positions in two DIRECT captures.
#   • MAP FADE: launch an app and grim DIRECT ~60-90ms later → the window is mid-fade
#     (alpha < 1), i.e. dimmer/translucent vs the settled capture. The fade ALSO shows
#     in the hart log only indirectly; the alpha math is unit-tested. Here we capture
#     a mid-map frame and the settled frame for side-by-side.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6c-hart.log
SWAY_LOG=/tmp/m6c-sway.log
SWAY_CFG=/tmp/m6c-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m6_artifacts
mkdir -p "$OUTDIR"
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m6" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
sleep 1.0
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" 2>/dev/null || true
: > "$SWAY_LOG"; chown sathish:sathish "$SWAY_LOG"; : > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true

cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1; exec sway -c '$SWAY_CFG' -d >> '$SWAY_LOG' 2>&1" &
HOST_SOCK=""
for i in $(seq 1 60); do HOST_SOCK=$(strip_ansi <"$SWAY_LOG" | grep -oE "wayland-[0-9]+" | tail -1); [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break; sleep 0.25; done
[ -z "$HOST_SOCK" ] && { echo FAILhost; exit 1; }
echo "[m6c] host $HOST_SOCK"
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART_SOCK=""
for i in $(seq 1 60); do HART_SOCK=$(strip_ansi <"$HART_LOG" | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25; done
[ -z "$HART_SOCK" ] && { echo FAILhart; tail -20 "$HART_LOG"; exit 1; }
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[m6c] hart $HART_SOCK"

GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/m6c-grim.err; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1.png" && echo "[m6c] -> $1.png ($(stat -c%s "$2"))"; }
WARP() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" swaymsg "seat - cursor set $1 $2" >/dev/null 2>&1; }

# Launch foot + IMMEDIATELY capture a mid-fade frame, then a settled one.
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot >/tmp/m6c-foot.log 2>&1 &
# Poll for the map, then grab within the 150ms fade window.
for i in $(seq 1 40); do strip_ansi <"$HART_LOG" | grep -q "window.opened" && break; sleep 0.03; done
GD /tmp/m6c-fade.png; SV F1-map-fade-midframe /tmp/m6c-fade.png   # ~immediately after map
sleep 0.5
GD /tmp/m6c-settled.png; SV F2-map-settled /tmp/m6c-settled.png    # fully opaque

# Cursor: warp to two spots; DIRECT-capture each (arrow follows the pointer in hart-comp).
WARP 320 240; sleep 0.25; GD /tmp/m6c-cur1.png; SV CUR1-arrow-at-320x240 /tmp/m6c-cur1.png
WARP 980 620; sleep 0.25; GD /tmp/m6c-cur2.png; SV CUR2-arrow-at-980x620 /tmp/m6c-cur2.png

echo ""
echo "=== cursor-pixel presence near the warp targets (proves the arrow moved) ==="
runuser -u sathish -- python3 /tmp/m6_curfind.py /tmp/m6c-cur1.png 320 240 /tmp/m6c-cur2.png 980 620 2>&1

echo ""
echo "=== hart log (map + any motion/ContextLost) ==="
strip_ansi <"$HART_LOG" | grep -iE "window.opened|ContextLost|initialized" | tail -8
echo "=== DONE ==="
