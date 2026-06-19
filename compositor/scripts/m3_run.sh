#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M3 — native app windows: multi xdg-shell + XWayland + focus, then a
# grim screenshot of the SWAY HOST (which composites hart-comp full-screen).
# ════════════════════════════════════════════════════════════════════════════
# Topology (proven in M1/M2):
#   headless sway 1.7 (non-root sathish, WLR_BACKENDS=headless, pixman)   ← HOST
#     └── hart-comp (winit, nested, creates its OWN wayland-N socket)
#           ├── foot              (xdg-shell terminal)
#           ├── gnome-calculator  (GTK4 xdg-shell)
#           └── xterm via XWayland (X11 → hart-comp's nested XWayland)
# hart-comp has NO zwlr_screencopy, so we grim the SWAY HOST (it shows hart-comp
# full-screen). That is the same proof path M1/M2 used.
set -u

RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m3-hartcomp.log
SWAY_LOG=/tmp/m3-sway-host.log
SWAY_CFG=/tmp/m3-sway.config
SHOT=/tmp/m3-shot.png
# Proof artifacts land here (mirrors compositor/m2_artifacts/). screenshot1 →
# m3-three-windows.png, screenshot2 (after typing) → m3-three-windows_typed.png.
OUT=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m3_artifacts/m3-three-windows.png

# ── 0. Clean slate (kill every stale actor so runs don't race for sockets) ──
pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m3" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
pkill -9 -u sathish xterm 2>/dev/null || true
pkill -9 Xwayland 2>/dev/null || true
sleep 1.0

# The user-session runtime dir may not exist on a fresh WSL boot. WLR needs an
# XDG_RUNTIME_DIR owned by the running user, mode 0700, to place its socket +
# lockfiles. Create it (idempotent) so headless sway can bind `wayland-host`.
mkdir -p "$RUNTIME"
chown sathish:sathish "$RUNTIME"
chmod 700 "$RUNTIME"
# Drop any stale sockets/locks from a previous run that sathish can't overwrite.
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true

# WSLg mounts /tmp/.X11-unix as a READ-ONLY tmpfs (it holds WSLg's own X0). Our
# nested XWayland needs to create its OWN X<n> socket there, which fails with
# "Read-only file system". Shadow it with a fresh writable tmpfs (session-local;
# idempotent — skip if already rw). This is the WSLg-specific unblock for XWayland.
if mount | grep -q '/tmp/.X11-unix type tmpfs (ro'; then
  mount -t tmpfs -o rw,nosuid,nodev tmpfs /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
  echo "[m3] remounted /tmp/.X11-unix rw (was RO under WSLg)"
fi
# NOTE: under WSL's /tmp mount root canNOT redirect into a sathish-owned file (no
# DAC override). So we do NOT pre-create the sathish-written logs here — each
# runuser shell opens its own fd as sathish. Just remove any stale copies.
rm -f "$HART_LOG" "$SWAY_LOG" 2>/dev/null || true

# Stage the binary world-readable (sathish cannot traverse /root, mode 700).
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"
chmod 755 "$HART_BIN"
chown sathish:sathish "$HART_BIN" 2>/dev/null || true

# ── 1. Host: headless sway. Keep XWayland OFF on the HOST (hart-comp spawns its
#       OWN nested XWayland); the host only needs to composite + forward input. ──
cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF

echo "[m3] starting headless sway host ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME
  export WLR_BACKENDS=headless
  export WLR_RENDERER=pixman
  export WLR_RENDERER_ALLOW_SOFTWARE=1
  export LIBGL_ALWAYS_SOFTWARE=1
  export WLR_HEADLESS_OUTPUTS=1
  exec sway -c '$SWAY_CFG' -d > '$SWAY_LOG' 2>&1
" &
# Discover the host socket name from the sway log (sway auto-names it wayland-N;
# it ignores WAYLAND_DISPLAY for its OWN server socket).
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }
HOST_SOCK=""
for i in $(seq 1 60); do
  HOST_SOCK=$(strip_ansi <"$SWAY_LOG" 2>/dev/null \
              | grep -oE "Running compositor on wayland display '[^']+'" \
              | grep -oE "wayland-[0-9]+" | tail -1)
  [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break
  sleep 0.25
done
if [ -z "$HOST_SOCK" ]; then
  echo "FAILED: host sway socket never appeared"; tail -30 "$SWAY_LOG"; exit 1
fi
echo "[m3] host sway up (host socket: $HOST_SOCK)"

# ── 2. Nest hart-comp in the host. It creates its OWN socket (wayland-N). ──
echo "[m3] launching hart-comp nested in the host ($HOST_SOCK) ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME
  export WAYLAND_DISPLAY=$HOST_SOCK
  export WLR_RENDERER_ALLOW_SOFTWARE=1
  export LIBGL_ALWAYS_SOFTWARE=1
  export HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1
  # XWayland 23.2 (built from source into /opt/xwayland-new — jammy ships only 22.1.1,
  # which lacks the modern xwayland-shell association protocol Smithay needs to attach a
  # wl_surface to an X11 window, so X11 windows would map-but-never-paint). Smithay spawns
  # the X server via Command::new(\"Xwayland\") + a PATH lookup, so the newer one MUST be
  # first on PATH. Its libwayland-1.22 dep is baked in via rpath (host is 1.20).
  export PATH=/opt/xwayland-new:\$PATH
  # Diagnostics OFF for the clean proof run. To debug, export HART_COMP_XWAYLAND_VERBOSE=1
  # (XWayland child stdio) and/or HART_COMP_DEBUG_RENDER=1 (per-element render dump)
  # before invoking this script.
  export NO_COLOR=1
  exec '$HART_BIN' --force-software > '$HART_LOG' 2>&1
" &
echo "[m3] hart-comp pid-group $!"

HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null \
              | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' \
              | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break
  sleep 0.25
done
if [ -z "$HART_SOCK" ]; then
  echo "FAILED: hart-comp did not announce a socket"; tail -40 "$HART_LOG"; exit 1
fi
echo "[m3] hart-comp OWN socket: $HART_SOCK"

# Wait for XWayland Ready (hart-comp spawns it) → learn its DISPLAY.
XDISP=""
for i in $(seq 1 60); do
  XDISP=$(strip_ansi <"$HART_LOG" 2>/dev/null \
          | grep -oE 'x11_display="?:[0-9]+' | grep -oE ':[0-9]+' | tail -1)
  [ -n "$XDISP" ] && break
  sleep 0.25
done
if [ -n "$XDISP" ]; then
  echo "[m3] XWayland ready on DISPLAY=$XDISP"
else
  echo "[m3] WARN: XWayland Ready not seen yet (will still try DISPLAY=:1)"
  XDISP=":1"
fi

launch_client() { # $1=label  $2..=argv
  local label="$1"; shift
  setsid runuser -u sathish -- env \
    XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" \
    WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 \
    GDK_BACKEND=wayland NO_AT_BRIDGE=1 \
    "$@" >"/tmp/m3-$label.log" 2>&1 &
  echo "[m3] launched $label (pid-group $!)"
}
launch_x11_client() { # $1=label $2..=argv  (DISPLAY only, no WAYLAND_DISPLAY so it uses XWayland)
  local label="$1"; shift
  setsid runuser -u sathish -- env \
    XDG_RUNTIME_DIR=$RUNTIME DISPLAY="$XDISP" \
    "$@" >"/tmp/m3-$label.log" 2>&1 &
  echo "[m3] launched x11:$label (pid-group $!) on DISPLAY=$XDISP"
}

# ── 3. Launch the three apps. xterm (X11) + calc first, foot LAST so foot is the
#       auto-focused window on map (proves focus-on-map + keyboard-to-focused). ──
launch_x11_client xterm xterm -geometry 50x14 -bg '#0a1a2a' -fg '#7fe0ff' \
  -fa 'Monospace' -fs 11 -title HARTOS-X11-xterm -e sh -c 'echo "  HARTOS XWayland (X11) window  "; echo "  win mapped via hart-comp XwmHandler  "; exec sh'
sleep 2.0
launch_client calc gnome-calculator
sleep 1.8
launch_client foot foot
sleep 2.0

echo "=== hart-comp log (map / focus / xwayland) ==="
strip_ansi <"$HART_LOG" | grep -iE 'window.opened|XWayland ready|X11 WM|render error|listening on its own' | tail -40

# ── 4a. Screenshot #1 — three windows mapped + cascaded. ──
grim_host() { # $1 = output path
  runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
    grim "$1" 2>/tmp/m3-grim.err || { echo "grim failed"; cat /tmp/m3-grim.err; }
}
echo "[m3] grim #1 (three windows) ..."
grim_host "$SHOT"
[ -f "$SHOT" ] && cp "$SHOT" "$OUT" && echo "[m3] screenshot1 -> $OUT ($(stat -c%s "$SHOT") bytes)"

# ── 4b. KEYBOARD proof: foot mapped last → it is the focused surface. Inject keys
#        via wtype INTO the host sway (the host forwards keyboard to its single
#        fullscreen window = hart-comp, whose winit input routes to the focused
#        surface = foot). If foot shows the typed text, keyboard-to-focused works. ──
echo "[m3] wtype into the focused window (foot) via the host sway ..."
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
  wtype "echo HART_M3_KEYBOARD_OK" 2>/tmp/m3-wtype.err || { echo "wtype failed:"; cat /tmp/m3-wtype.err; }
sleep 0.6
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
  wtype -k Return 2>>/tmp/m3-wtype.err || true
sleep 0.8
echo "[m3] grim #2 (after typing into foot) ..."
grim_host "${SHOT%.png}_typed.png"
[ -f "${SHOT%.png}_typed.png" ] && cp "${SHOT%.png}_typed.png" "${OUT%.png}_typed.png" \
  && echo "[m3] screenshot2 -> ${OUT%.png}_typed.png ($(stat -c%s "${SHOT%.png}_typed.png") bytes)"

echo "=== hart-comp keyboard/focus log tail ==="
strip_ansi <"$HART_LOG" | grep -iE 'window.opened|focus|keyboard|key' | tail -10

echo "=== DONE ==="
