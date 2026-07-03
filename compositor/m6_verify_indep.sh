#!/usr/bin/env bash
# INDEPENDENT M6 verification — captures DIRECT from hart-comp (NOT the host workaround).
# Verifier-authored. Boots headless sway as sathish, nests hart-comp, launches apps,
# then exercises screencopy/cursor/animations/killswitch ALL via DIRECT grim on $HART_SOCK.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/v6-hart.log
SWAY_LOG=/tmp/v6-sway.log
SWAY_CFG=/tmp/v6-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m6_verify
mkdir -p "$OUTDIR"
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/v6" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
sleep 1.0
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" 2>/dev/null || true
: > "$SWAY_LOG"; chown sathish:sathish "$SWAY_LOG"; : > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
# Fresh copy of the freshly-built binary
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true
echo "[v6] binary md5: $(md5sum "$HART_BIN" | cut -d' ' -f1)  built: $(stat -c%y /root/hart-comp/target/debug/hart-comp)"

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
echo "[v6] host sway socket: $HOST_SOCK"

setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 HART_COMP_DEBUG_FADE=1 NO_COLOR=1; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART_SOCK=""
for i in $(seq 1 60); do HART_SOCK=$(strip_ansi <"$HART_LOG" | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25; done
[ -z "$HART_SOCK" ] && { echo FAILhart; tail -30 "$HART_LOG"; exit 1; }
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[v6] hart-comp socket: $HART_SOCK   ipc: $([ -S "$IPC_SOCK" ] && echo UP || echo DOWN)"

# Confirm hart-comp ADVERTISES zwlr_screencopy via wayland-info (DIRECT registry dump)
echo "── registry: does hart-comp advertise zwlr_screencopy_manager_v1? ──"
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" wayland-info 2>/dev/null | grep -iE 'screencopy|wlr_layer|xdg_wm|compositor' | head -20 || echo "(wayland-info unavailable)"

GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/v6-grimd.err; echo "grim_exit=$?"; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1.png" && echo "[v6] saved $1.png ($(stat -c%s "$2") bytes)"; }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" "$@"; }

# Launch 2 apps to give the framebuffer real content
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot >/tmp/v6-foot1.log 2>&1 &
sleep 1.0
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot >/tmp/v6-foot2.log 2>&1 &
sleep 1.2

echo ""
echo "════ 1. SCREENCOPY DIRECT ════"
GD /tmp/v6-direct.png
SV 1-direct-capture /tmp/v6-direct.png

echo ""
echo "════ 2. KILLSWITCH ON (direct grim should be BLACK or REFUSED) ════"
IPC screen.kill on
GD /tmp/v6-killed.png
SV 2-killed-direct /tmp/v6-killed.png

echo ""
echo "════ 3. KILLSWITCH OFF (direct grim returns) ════"
IPC screen.kill off
sleep 0.4
GD /tmp/v6-restored.png
SV 3-restored-direct /tmp/v6-restored.png

echo ""
echo "════ 4. window.list via IPC (cursor/state sanity) ════"
IPC window.list 2>/dev/null || echo "(window.list not a verb)"

echo ""
echo "════ luminance analysis (direct captures) ════"
runuser -u sathish -- python3 /tmp/m6_lum.py /tmp/v6-direct.png /tmp/v6-killed.png /tmp/v6-restored.png 2>/dev/null || echo "(lum script failed / files missing)"

echo ""
echo "════ FADE / animation log (HART_COMP_DEBUG_FADE) ════"
strip_ansi <"$HART_LOG" | grep -iE 'fade|alpha|MapAnim|FADE_IN|ws_switch|crossfade' | tail -20

echo ""
echo "════ screencopy / killswitch / cursor log lines ════"
strip_ansi <"$HART_LOG" | grep -iE 'screencopy|screen.kill|capture|cursor|killswitch|blocked' | tail -25

echo ""
echo "════ direct grim sizes ════"
for f in /tmp/v6-direct.png /tmp/v6-killed.png /tmp/v6-restored.png; do printf "  %s: " "$(basename $f)"; [ -f "$f" ] && stat -c%s "$f" || echo MISSING; echo; done
echo "════ grim stderr (direct) ════"; tail -10 /tmp/v6-grimd.err 2>/dev/null
echo "=== V6 DONE ==="
