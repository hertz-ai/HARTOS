#!/usr/bin/env bash
# Independent verification: isolate whether the XWayland (X11) xterm actually
# PAINTS in hart-comp (the M3 grim showed only calc+foot). Uses HART_COMP_DEBUG_RENDER
# to dump each space element's loc/size/buffer, launches ONLY the X11 xterm.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m3-hartcomp-dbg.log
SWAY_LOG=/tmp/m3-sway-dbg.log
SWAY_CFG=/tmp/m3-sway-dbg.config
OUT=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m3_artifacts/m3-x11only.png

strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m3" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
pkill -9 -u sathish xterm 2>/dev/null || true
pkill -9 Xwayland 2>/dev/null || true
sleep 1

mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
if mount | grep -q '/tmp/.X11-unix type tmpfs (ro'; then
  mount -t tmpfs -o rw,nosuid,nodev tmpfs /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
fi
rm -f "$HART_LOG" "$SWAY_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"
chown sathish:sathish "$HART_BIN" 2>/dev/null || true

cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF

setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1
  exec sway -c '$SWAY_CFG' -d > '$SWAY_LOG' 2>&1
" &
HOST_SOCK=""
for i in $(seq 1 60); do
  HOST_SOCK=$(strip_ansi <"$SWAY_LOG" 2>/dev/null | grep -oE "wayland-[0-9]+" | tail -1)
  [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break
  sleep 0.25
done
echo "host=$HOST_SOCK"

setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1 HART_COMP_DEBUG_RENDER=1 NO_COLOR=1
  # XWayland 23.2 (built into /opt/xwayland-new) — the modern xwayland-shell association
  # path; jammy's stock 22.1.1 only does the legacy WL_SURFACE_ID path Smithay cannot
  # complete, so X11 windows map-but-never-paint with it. Must be first on PATH.
  export PATH=/opt/xwayland-new:\$PATH
  exec '$HART_BIN' --force-software > '$HART_LOG' 2>&1
" &
HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break
  sleep 0.25
done
echo "hart=$HART_SOCK"
XDISP=""
for i in $(seq 1 40); do
  XDISP=$(strip_ansi <"$HART_LOG" 2>/dev/null | grep -oE 'x11_display=:?[0-9]+' | grep -oE ':?[0-9]+' | tail -1)
  [ -n "$XDISP" ] && break
  sleep 0.25
done
case "$XDISP" in :*) ;; *) XDISP=":$XDISP";; esac
echo "xdisp=$XDISP"

setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME DISPLAY="$XDISP" \
  xterm -geometry 60x16 -bg '#0a1a2a' -fg '#7fe0ff' -title HARTOS-X11 \
  -e sh -c 'echo XTERM_IS_LIVE_ON_HARTCOMP; exec sh' >/tmp/m3-xterm-dbg.log 2>&1 &
sleep 3.5

echo "=== render.element dump (X11 paint diagnosis) ==="
strip_ansi <"$HART_LOG" | grep -E 'render.element|window.opened|mapped X11|X11 WM|XWayland ready' | tail -25
echo "=== xterm client log ==="
tail -5 /tmp/m3-xterm-dbg.log
echo "=== live procs ==="
ps -u sathish -o pid,comm | grep -iE 'xterm|Xwayland|hart-comp' || echo none

runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
  grim /tmp/m3-x11only.png 2>/tmp/grim-dbg.err \
  && echo "shot ok: $(stat -c%s /tmp/m3-x11only.png) bytes" || cat /tmp/grim-dbg.err
cp /tmp/m3-x11only.png "$OUT" 2>/dev/null && echo "copied -> $OUT"
echo DONE
