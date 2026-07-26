#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp FULL INTEGRATION — all 9 milestones in ONE live winit session.
# Topology:
#   headless sway 1.7 (sathish, WLR_BACKENDS=headless, pixman)         <- HOST
#     '-- hart-comp (winit, nested; own wayland-N socket + IPC socket)  <- THE OS
#           |-- glass-shell layer host (root client, BACKGROUND layer)  M2
#           |-- foot                  (xdg-shell terminal)              M3
#           |-- gnome-calculator      (GTK4 xdg-shell)                  M3
#           '-- xterm via XWayland    (X11 -> hart-comp's nested Xwm)   M3
#   -- exercised in-session: M4 IPC arrange, M5 workspaces+snap+chords,
#      M6 cursor+fade+screencopy+killswitch. DIRECT grim on hart-comp.   --
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/integ-hart.log
SWAY_LOG=/tmp/integ-sway.log
SWAY_CFG=/tmp/integ-sway.config
GLASS_LOG=/tmp/integ-glass.log
GLASS_STATUS=/tmp/integ-glass-status.log
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/integ_artifacts
mkdir -p "$OUTDIR"
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

echo "############ CLEAN SLATE ############"
pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -f "integ_glass_host.py" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/integ" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
pkill -9 xterm 2>/dev/null || true
pkill -9 Xwayland 2>/dev/null || true
sleep 1.2
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" "$GLASS_STATUS" 2>/dev/null || true
: > "$SWAY_LOG"; chown sathish:sathish "$SWAY_LOG"
: > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true
echo "[integ] binary md5: $(md5sum "$HART_BIN" | cut -d' ' -f1)  built: $(stat -c%y /root/hart-comp/target/debug/hart-comp)"

# -- 1. Headless sway HOST as sathish --
cat >"$SWAY_CFG" <<EOF
output HEADLESS-1 mode 1440x900 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1; exec sway -c '$SWAY_CFG' -d >> '$SWAY_LOG' 2>&1" &
HOST_SOCK=""
for i in $(seq 1 80); do HOST_SOCK=$(strip_ansi <"$SWAY_LOG" | grep -oE "wayland-[0-9]+" | tail -1); [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break; sleep 0.25; done
[ -z "$HOST_SOCK" ] && { echo "FAIL: host sway socket"; tail -20 "$SWAY_LOG"; exit 1; }
echo "[integ] HOST sway socket: $HOST_SOCK"

# -- 2. Nest hart-comp (winit) as sathish — direct screencopy path --
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 HART_COMP_DEBUG_FADE=1 HART_COMP_DEBUG_KEYS=1 NO_COLOR=1 PATH=/opt/xwayland-new:\$PATH; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART_SOCK=""
for i in $(seq 1 80); do HART_SOCK=$(strip_ansi <"$HART_LOG" | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25; done
[ -z "$HART_SOCK" ] && { echo "FAIL: hart-comp socket"; tail -30 "$HART_LOG"; exit 1; }
for i in $(seq 1 50); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[integ] HART-comp socket: $HART_SOCK   IPC: $([ -S "$IPC_SOCK" ] && echo UP || echo DOWN)"
chmod o+rwx "$RUNTIME/$HART_SOCK" 2>/dev/null || true
chmod o+rwx "$IPC_SOCK" 2>/dev/null || true
chmod o+rx "$RUNTIME" 2>/dev/null || true

# Learn the XWayland DISPLAY hart-comp spawned (for the X11 app).
XDISP=""
for i in $(seq 1 40); do XDISP=$(strip_ansi <"$HART_LOG" | grep -oE ':[0-9]+' | grep -E '^:[0-9]+$' | tail -1); [ -n "$XDISP" ] && break; sleep 0.2; done
[ -z "$XDISP" ] && XDISP=":1"
echo "[integ] XWayland DISPLAY guess: $XDISP"

echo ""
echo "-- registry: hart-comp advertises (compositor/layer-shell/xdg/screencopy)? --"
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" wayland-info 2>/dev/null | grep -iE 'screencopy|wlr_layer|xdg_wm|wl_compositor|xwayland' | head -12 || echo "(wayland-info unavailable)"

# Helpers
GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/integ-grim.err; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1" && echo "[integ] saved $1 ($(stat -c%s "$2") bytes)"; }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" "$@"; }
IPC6() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/m6_ipc.py "$IPC_SOCK" "$@"; }

echo ""
echo "############ 3. BRING UP THE FULL DESKTOP ############"
# -- M2: glass-shell layer host as ROOT client (BACKGROUND layer) --
GLASS_ENV="export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HART_SOCK GI_TYPELIB_PATH=/usr/local/lib/x86_64-linux-gnu/girepository-1.0 LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu LD_PRELOAD=/usr/local/lib/x86_64-linux-gnu/libgtk4-layer-shell.so WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 WEBKIT_FORCE_SANDBOX=0 WEBKIT_DISABLE_DMABUF_RENDERER=1 WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland XDG_DATA_DIRS=/usr/local/share:/usr/share HART_REPO=/mnt/c/Users/sathi/PycharmProjects/HARTOS HART_LIQUID_PORT=6800 HART_DATA_DIR=/tmp/integ-hart-data HART_GLASS_STATUS=$GLASS_STATUS"
setsid bash -c "$GLASS_ENV; exec python3 -u /tmp/integ/integ_glass_host.py >> '$GLASS_LOG' 2>&1" &
echo "[integ] glass host launched (root client). Waiting for WebView FINISHED..."
for i in $(seq 1 140); do grep -q "WebView load FINISHED" "$GLASS_STATUS" 2>/dev/null && break; sleep 0.5; done
echo "-- glass status --"; cat "$GLASS_STATUS" 2>/dev/null
sleep 2.0
GD /tmp/integ-A-shell.png; SV 01-glass-shell-desktop.png /tmp/integ-A-shell.png

# -- M3: native app windows: foot + gnome-calculator + xterm(XWayland) --
echo ""
echo "-- launching native apps (foot, gnome-calculator, xterm via XWayland) --"
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot -T HARTOS-foot >/tmp/integ-foot.log 2>&1 &
sleep 1.5
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 gnome-calculator >/tmp/integ-calc.log 2>&1 &
sleep 2.0
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME DISPLAY="$XDISP" xterm -geometry 54x16 -bg "#0a1a2a" -fg "#7fe0ff" -fa Monospace -fs 11 -title HARTOS-X11-xterm -e sh -c 'echo "  HARTOS XWayland (X11) window"; echo "  mapped via hart-comp XwmHandler"; exec sh' >/tmp/integ-xterm.log 2>&1 &
sleep 2.5
GD /tmp/integ-B-apps.png; SV 02-apps-launched-untiled.png /tmp/integ-B-apps.png

echo ""
echo "############ 4. EXERCISE EVERY MILESTONE — SAME SESSION ############"
echo "======== M4 . com.hart.Compositor arranges REAL windows (THE MOAT) ========"
echo "-- window.list (the apps OVER the glass-shell BACKGROUND layer) --"
IPC list | tee /tmp/integ-list-before.txt

echo ""
echo "-- window.tile grid (the moat arranges) --"
IPC tile grid
sleep 1.2
GD /tmp/integ-C-tilegrid.png; SV 03-M4-tile-grid.png /tmp/integ-C-tilegrid.png
echo "-- window.list after tile --"
IPC list | tee /tmp/integ-list-tiled.txt

echo ""
echo "-- window.tile master-stack --"
IPC tile master-stack
sleep 1.0
GD /tmp/integ-C2-master.png; SV 04-M4-tile-master-stack.png /tmp/integ-C2-master.png

echo ""
echo "-- window.focus first handle --"
H1=$(runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" raw '{"method":"window.list","args":{}}' | python3 -c "import sys,json
s=sys.stdin.read()
try:
    r=json.loads(s.split('-> ',1)[1]); w=r['result']['windows']; print(w[0]['handle'] if w else '')
except Exception:
    print('')" 2>/dev/null)
echo "[integ] first handle = $H1"
[ -n "$H1" ] && IPC focus "$H1"

echo ""
echo "======== M5 . workspaces + snap-zones + keybinding chords ========"
echo "-- window.place LEFT (snap zone) on $H1 --"
[ -n "$H1" ] && IPC place "$H1" left
sleep 0.8
GD /tmp/integ-D-snapleft.png; SV 05-M5-snap-left.png /tmp/integ-D-snapleft.png

echo ""
echo "-- workspace.switch 2 (apps on WS1 should HIDE) --"
IPC6 workspace.switch 2
sleep 1.0
GD /tmp/integ-E-ws2.png; SV 06-M5-workspace2-empty.png /tmp/integ-E-ws2.png
echo "-- window.list on WS2 (should be empty/hidden) --"
IPC list | tee /tmp/integ-list-ws2.txt

echo ""
echo "-- move $H1 to workspace 2 then confirm it appears --"
[ -n "$H1" ] && runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" raw "{\"method\":\"window.move_to_workspace\",\"args\":{\"handle\":\"$H1\",\"workspace\":2}}"
sleep 0.8
GD /tmp/integ-E2-ws2-moved.png; SV 07-M5-window-moved-to-ws2.png /tmp/integ-E2-ws2-moved.png
IPC list | tee /tmp/integ-list-ws2-after.txt

echo ""
echo "-- workspace.switch 1 (restore the original set) --"
IPC6 workspace.switch 1
sleep 1.0
GD /tmp/integ-E3-ws1back.png; SV 08-M5-workspace1-restored.png /tmp/integ-E3-ws1back.png

echo ""
echo "-- keybinding chords via wtype (Super+2 switch ws, Alt+Tab focus). Note if unreliable. --"
WT() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" wtype "$@" 2>&1; echo "wtype_rc=$?"; }
echo "  Super+2:"; WT -M logo -k 2 -m logo
sleep 0.7
echo "  Super+1:"; WT -M logo -k 1 -m logo
sleep 0.5
echo "  Alt+Tab:"; WT -M alt -k Tab -m alt
sleep 0.5
echo "-- hart-comp key/chord log (HART_COMP_DEBUG_KEYS) --"
strip_ansi <"$HART_LOG" | grep -iE 'chord|keybind|key press|Super|Logo|Alt|workspace.*key|wtype' | tail -15

echo ""
echo "======== M6 . effects (cursor render, fade) + screencopy + killswitch ========"
echo "-- direct screencopy already proven (every GD above is a DIRECT grim on hart-comp) --"
GD /tmp/integ-F-cursor.png; SV 09-M6-direct-with-cursor.png /tmp/integ-F-cursor.png
echo "-- cursor presence (curfind) --"
runuser -u sathish -- python3 /tmp/integ/curfind.py /tmp/integ-F-cursor.png 2>/dev/null || echo "(curfind unavailable)"

echo ""
echo "-- window-open FADE: launch a NEW foot, capture mid-fade, check fade log --"
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot -T HARTOS-fade-probe >/tmp/integ-foot-fade.log 2>&1 &
sleep 0.18
GD /tmp/integ-G-midfade.png; SV 10-M6-window-open-fade.png /tmp/integ-G-midfade.png
sleep 1.2
echo "-- fade/anim log lines --"
strip_ansi <"$HART_LOG" | grep -iE 'fade|alpha|MapAnim|FADE_IN|crossfade|ws_switch|ws_fade' | tail -15

echo ""
echo "-- KILLSWITCH ON: direct grim must be REFUSED or BLACK + input blocked --"
IPC6 screen.kill on
GD /tmp/integ-H-killed.png; SV 11-M6-killswitch-on.png /tmp/integ-H-killed.png
echo "-- while killed: try an input chord (should be blocked) --"
WT -k a
echo "-- KILLSWITCH OFF: direct grim returns --"
IPC6 screen.kill off
sleep 0.6
GD /tmp/integ-I-restored.png; SV 12-M6-killswitch-off-restored.png /tmp/integ-I-restored.png

echo ""
echo "-- luminance: normal vs killed vs restored --"
runuser -u sathish -- python3 /tmp/integ/lum.py /tmp/integ-F-cursor.png /tmp/integ-H-killed.png /tmp/integ-I-restored.png 2>/dev/null || echo "(lum failed)"

echo ""
echo "############ 5. FINAL COMPOSITE: glass backdrop + tiled apps + cursor ############"
IPC tile grid
sleep 1.2
GD /tmp/integ-FINAL.png; SV 99-FINAL-full-desktop.png /tmp/integ-FINAL.png
echo "-- FINAL window.list --"
IPC list | tee /tmp/integ-list-final.txt

echo ""
echo "############ KILL-SWITCH / SCREENCOPY LOG SUMMARY ############"
strip_ansi <"$HART_LOG" | grep -iE 'screencopy|screen.kill|capture|cursor|killswitch|blocked|refused' | tail -25
echo ""
echo "############ MAP / FOCUS / XWAYLAND LOG SUMMARY ############"
strip_ansi <"$HART_LOG" | grep -iE 'map|focus|xwayland|xwm|toplevel|layer' | tail -20
echo ""
echo "############ ARTIFACTS ############"
ls -la "$OUTDIR"/
echo ""
echo "############ GRIM STDERR (any failures) ############"
tail -15 /tmp/integ-grim.err 2>/dev/null
echo "=== INTEGRATION DRIVER DONE ==="
