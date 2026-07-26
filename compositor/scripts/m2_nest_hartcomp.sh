#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M2 — nest hart-comp inside the headless sway host, then probe its
# OWN socket for the wlr-layer-shell global. Run as root; drops to sathish.
# ════════════════════════════════════════════════════════════════════════════
# The host sway socket is wayland-1 (under /run/user/1000). hart-comp's winit
# backend connects to THAT as its host, and creates its OWN socket (wayland-2,
# wayland-3, ... auto). We grep hart-comp's log for "listening on its own wayland
# socket" to learn that name, then run weston-info against it.
set -u

# Kill any stale hart-comp BINARY (match the exe path, NOT this script's path —
# `pkill -f hart-comp` would match our own argv and suicide).
pkill -f "/tmp/hart-comp" 2>/dev/null || true
sleep 0.5

# The build lives under /root (mode 700) which `sathish` cannot traverse, so we
# stage the freshly-built binary into a sathish-readable path each run.
cp /root/hart-comp/target/debug/hart-comp /tmp/hart-comp
chmod 755 /tmp/hart-comp
chown sathish:sathish /tmp/hart-comp
HART_BIN=/tmp/hart-comp
HART_LOG=/tmp/m2-hartcomp.log
# NOTE: under WSL's /tmp, root canNOT redirect into a sathish-owned file (no DAC
# override on this mount). So the redirect is performed INSIDE the sathish shell
# (it owns the fd it opens). We only rm here.
rm -f "$HART_LOG"

# Launch hart-comp as sathish, nested in the sway host (wayland-1), software GL.
# HART_COMP_NO_TEST_CLIENT=1 suppresses the auto foot client so the ONLY clients
# are the ones we attach deliberately (swaybg / the WebKit host).
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=/run/user/1000
  export WAYLAND_DISPLAY=wayland-1
  export WLR_RENDERER_ALLOW_SOFTWARE=1
  export LIBGL_ALWAYS_SOFTWARE=1
  export HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1
  export NO_COLOR=1
  exec '$HART_BIN' --force-software > '$HART_LOG' 2>&1
" &
echo "launched hart-comp (pid-group $!) nested in host wayland-1"

# Wait for hart-comp to announce its own socket. Strip ANSI escapes first (tracing
# colorizes even with NO_COLOR on some builds), then match the announce line; the
# host socket (wayland-1) is excluded so we only pick hart-comp's OWN (wayland-2+).
HART_SOCK=""
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }
for i in $(seq 1 40); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null \
              | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' \
              | grep -oE 'wayland-[0-9]+' | tail -1)
  if [ -z "$HART_SOCK" ]; then
    HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null \
                | grep -oE 'Created new socket.*wayland-[0-9]+' \
                | grep -oE 'wayland-[0-9]+' | tail -1)
  fi
  if [ -n "$HART_SOCK" ] && [ -S "/run/user/1000/$HART_SOCK" ]; then
    echo "HART-comp own socket: $HART_SOCK"
    break
  fi
  sleep 0.25
done

if [ -z "$HART_SOCK" ]; then
  echo "FAILED: hart-comp did not announce a socket"
  tail -30 "$HART_LOG"
  exit 1
fi
echo "$HART_SOCK" >/tmp/m2-hartcomp.sock
echo "=== hart-comp boot tail ==="
grep -E 'EGL Initialized|GL Renderer|listening on its own|initialized — entering the loop|render error' "$HART_LOG" | head -20

# ── PROBE: does hart-comp advertise zwlr_layer_shell_v1? ──
echo ""
echo "=== weston-info against hart-comp ($HART_SOCK) ==="
runuser -u sathish -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY="$HART_SOCK" \
  weston-info 2>/tmp/m2-weston-info.err > /tmp/m2-weston-info.txt || true
grep -iE 'zwlr_layer_shell|xdg_wm_base|wl_compositor|wl_shm|wl_seat|wl_output' /tmp/m2-weston-info.txt || cat /tmp/m2-weston-info.err
