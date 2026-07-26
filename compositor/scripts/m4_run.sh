#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M4 — the com.hart.Compositor IPC arranges REAL windows.
# ════════════════════════════════════════════════════════════════════════════
# Builds on the M3 topology (proven):
#   headless sway 1.7 (non-root sathish, WLR_BACKENDS=headless, pixman)   ← HOST
#     └── hart-comp (winit, nested, OWN wayland-N socket + hart-comp.sock IPC)
#           ├── foot              (xdg-shell terminal)
#           ├── gnome-calculator  (GTK4 xdg-shell)
#           └── xterm via XWayland (X11 → hart-comp's nested XWayland)
#
# Then THE MOAT: the m4_ipc_client.py speaks framed JSON to hart-comp.sock and
#   (a) lists the 3 windows, (b) tiles them into a grid, (c) focuses+raises one,
#   (d) moves/resizes one, (e) closes one — each captured BEFORE/AFTER with grim
#   of the SWAY HOST (which composites hart-comp full-screen; hart-comp has no
#   zwlr_screencopy, the same proof path M1/M2/M3 used).
set -u

RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m4-hartcomp.log
SWAY_LOG=/tmp/m4-sway-host.log
SWAY_CFG=/tmp/m4-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
CLIENT=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/scripts/m4_ipc_client.py
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m4_artifacts
mkdir -p "$OUTDIR"

strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

# ── 0. Clean slate ──
pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m4" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
pkill -9 -u sathish xterm 2>/dev/null || true
pkill -9 Xwayland 2>/dev/null || true
sleep 1.0

mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" 2>/dev/null || true
if mount | grep -q '/tmp/.X11-unix type tmpfs (ro'; then
  mount -t tmpfs -o rw,nosuid,nodev tmpfs /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
  echo "[m4] remounted /tmp/.X11-unix rw (was RO under WSLg)"
fi
rm -f "$HART_LOG" "$SWAY_LOG" 2>/dev/null || true

cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"
chmod 755 "$HART_BIN"
chown sathish:sathish "$HART_BIN" 2>/dev/null || true

# ── 1. Host: headless sway ──
cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF

echo "[m4] starting headless sway host ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1
  exec sway -c '$SWAY_CFG' -d > '$SWAY_LOG' 2>&1
" &
HOST_SOCK=""
for i in $(seq 1 60); do
  HOST_SOCK=$(strip_ansi <"$SWAY_LOG" 2>/dev/null \
              | grep -oE "Running compositor on wayland display '[^']+'" \
              | grep -oE "wayland-[0-9]+" | tail -1)
  [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break
  sleep 0.25
done
if [ -z "$HOST_SOCK" ]; then echo "FAILED: host sway socket never appeared"; tail -30 "$SWAY_LOG"; exit 1; fi
echo "[m4] host sway up (socket: $HOST_SOCK)"

# ── 2. Nest hart-comp (creates its OWN wayland-N socket + binds hart-comp.sock) ──
echo "[m4] launching hart-comp nested in the host ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1
  export PATH=/opt/xwayland-new:\$PATH
  exec '$HART_BIN' --force-software > '$HART_LOG' 2>&1
" &
echo "[m4] hart-comp pid-group $!"

HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null \
              | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' \
              | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break
  sleep 0.25
done
if [ -z "$HART_SOCK" ]; then echo "FAILED: hart-comp did not announce a socket"; tail -40 "$HART_LOG"; exit 1; fi
echo "[m4] hart-comp OWN socket: $HART_SOCK"

# Wait for the IPC socket to appear (M4 deliverable).
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
if [ -S "$IPC_SOCK" ]; then
  echo "[m4] IPC socket up: $IPC_SOCK ($(stat -c '%a' "$IPC_SOCK") perms)"
else
  echo "FAILED: hart-comp.sock never appeared"; strip_ansi <"$HART_LOG" | grep -i ipc | tail; exit 1
fi

# XWayland DISPLAY
XDISP=""
for i in $(seq 1 60); do
  XDISP=$(strip_ansi <"$HART_LOG" 2>/dev/null | grep -oE 'x11_display="?:[0-9]+' | grep -oE ':[0-9]+' | tail -1)
  [ -n "$XDISP" ] && break; sleep 0.25
done
[ -z "$XDISP" ] && XDISP=":1"
echo "[m4] XWayland DISPLAY=$XDISP"

launch_client() { local label="$1"; shift
  setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" \
    WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland NO_AT_BRIDGE=1 \
    "$@" >"/tmp/m4-$label.log" 2>&1 &
  echo "[m4] launched $label (pid-group $!)"; }
launch_x11_client() { local label="$1"; shift
  setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME DISPLAY="$XDISP" \
    "$@" >"/tmp/m4-$label.log" 2>&1 &
  echo "[m4] launched x11:$label (pid-group $!)"; }

# ── 3. Launch the three apps (same set as M3). ──
launch_x11_client xterm xterm -geometry 50x14 -bg '#2a0a1a' -fg '#ff9fd0' \
  -fa 'Monospace' -fs 11 -title HARTOS-X11-xterm \
  -e sh -c 'echo "  HARTOS XWayland (X11) — arranged by the AGENT via IPC  "; exec sh'
sleep 2.0
launch_client calc gnome-calculator
sleep 1.8
launch_client foot foot
sleep 2.0

# grim helper (host composites hart-comp full-screen).
grim_host() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
    grim "$1" 2>/tmp/m4-grim.err || { echo "grim failed"; cat /tmp/m4-grim.err; }; }
shot() { local name="$1"; grim_host "/tmp/m4-$name.png"
  [ -f "/tmp/m4-$name.png" ] && cp "/tmp/m4-$name.png" "$OUTDIR/$name.png" \
    && echo "[m4] screenshot -> $OUTDIR/$name.png ($(stat -c%s /tmp/m4-$name.png) bytes)"; }

ipc() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 "$CLIENT" "$IPC_SOCK" "$@"; }

echo ""
echo "════════════ M4: THE AGENT ARRANGES REAL WINDOWS VIA IPC ════════════"

# (before) 3 windows cascaded by the compositor's default placement.
shot 01-before-cascade

# (a) LIST — enumerate the 3 real windows from state.space.
echo ""
echo "── (a) window.list ──"
ipc list | tee /tmp/m4-list-before.txt
# Extract handles in listing order (win_*) for the subsequent ops.
HANDLES=($(grep -oE 'win_[0-9a-f]+' /tmp/m4-list-before.txt | awk '!seen[$0]++'))
echo "[m4] handles: ${HANDLES[*]}"
if [ "${#HANDLES[@]}" -lt 3 ]; then echo "WARN: expected >=3 handles, got ${#HANDLES[@]}"; fi

# (b) TILE grid — arrange ALL windows into a grid (move+resize each).
echo ""
echo "── (b) window.tile grid ──"
ipc tile grid
sleep 1.0
shot 02-after-tile-grid

# (c) FOCUS+raise a specific window (the xterm/X11 one, last handle is foot-mapped;
#     pick the FIRST listed which is the bottom of the stack — raising proves z-order).
echo ""
echo "── (c) window.focus (raise the first-listed window to the top) ──"
ipc focus "${HANDLES[0]}"
sleep 0.8
shot 03-after-focus-raise

# (d) MOVE + RESIZE one window to an explicit rect (top-left quadrant, bigger).
echo ""
echo "── (d) window.move + window.resize one window ──"
ipc move "${HANDLES[1]}" 40 40
ipc resize "${HANDLES[1]}" 760 520
sleep 1.0
shot 04-after-move-resize

# (e) CLOSE one window (the last handle) — proves the window really goes away.
echo ""
echo "── (e) window.close one window ──"
ipc close "${HANDLES[2]}"
sleep 1.2
shot 05-after-close
echo ""
echo "── window.list AFTER close (should be one fewer) ──"
ipc list | tee /tmp/m4-list-after.txt

echo ""
echo "=== hart-comp IPC log ==="
strip_ansi <"$HART_LOG" | grep -iE 'IPC|window.opened|window.closed|listening on its own' | tail -30

echo ""
echo "=== M4 DONE ==="
echo "before handles: ${HANDLES[*]}"
echo "windows before close: $(grep -c 'win_' /tmp/m4-list-before.txt)"
echo "windows after close:  $(grep -c 'win_' /tmp/m4-list-after.txt)"
