#!/usr/bin/env bash
# M6 fast-cadence proof — front-loads the killswitch + crossfade BEFORE the nested
# WSL EGL surface can go ContextLost (~5s window observed). Assumes m6_run.sh already
# brought the stack up OR brings a fresh one up quickly. Self-contained: it boots the
# stack, then fires all captures back-to-back with minimal sleeps.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6p-hart.log
SWAY_LOG=/tmp/m6p-sway.log
SWAY_CFG=/tmp/m6p-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m6_artifacts
mkdir -p "$OUTDIR"
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m6" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
sleep 1.0
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" 2>/dev/null || true
: > "$SWAY_LOG"; chown sathish:sathish "$SWAY_LOG"
: > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true

cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1
  exec sway -c '$SWAY_CFG' -d >> '$SWAY_LOG' 2>&1
" &
HOST_SOCK=""
for i in $(seq 1 60); do
  HOST_SOCK=$(strip_ansi <"$SWAY_LOG" 2>/dev/null | grep -oE "wayland-[0-9]+" | tail -1)
  [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break; sleep 0.25
done
[ -z "$HOST_SOCK" ] && { echo "FAIL host"; tail -20 "$SWAY_LOG"; exit 1; }
echo "[m6p] host $HOST_SOCK"

setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1
  exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1
" &
HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25
done
[ -z "$HART_SOCK" ] && { echo "FAIL hart"; tail -30 "$HART_LOG"; exit 1; }
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[m6p] hart $HART_SOCK ipc=$([ -S "$IPC_SOCK" ] && echo up)"

GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/m6p-grim.err; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1.png" && echo "[m6p] -> $1.png ($(stat -c%s "$2"))"; }
LC() { local l="$1"; shift; setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland NO_AT_BRIDGE=1 "$@" >"/tmp/m6p-$l.log" 2>&1 & }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" "$@"; }

# Two apps; brief settle.
LC calc gnome-calculator; sleep 1.4
LC foot foot; sleep 1.4

echo "── A: windows (DIRECT) ──"; GD /tmp/m6p-A.png; SV A-windows /tmp/m6p-A.png

echo "── B: screen.kill ON → all-black (DIRECT) ──"; IPC screen.kill on; sleep 0.35
GD /tmp/m6p-B.png; SV B-killswitch-on /tmp/m6p-B.png

echo "── C: screen.kill OFF → windows return (DIRECT) ──"; IPC screen.kill off; sleep 0.45
GD /tmp/m6p-C.png; SV C-killswitch-off /tmp/m6p-C.png

echo "── D: workspace crossfade (ws2→ws1, capture mid-fade) ──"; IPC workspace.switch 2 >/dev/null; sleep 0.4; IPC workspace.switch 1 >/dev/null
GD /tmp/m6p-D.png; SV D-ws-crossfade /tmp/m6p-D.png

echo ""
echo "=== blackness analysis (proves the killswitch surface) ==="
runuser -u sathish -- python3 /tmp/m6_lum.py /tmp/m6p-A.png /tmp/m6p-B.png /tmp/m6p-C.png

echo ""
echo "=== hart M6 log ==="
strip_ansi <"$HART_LOG" | grep -iE 'screencopy|screen.kill|capture/input|workspace.switch|window.opened|ContextLost|render error' | tail -20
echo "=== M6 PROVE DONE ==="
ls -la "$OUTDIR"/*.png 2>/dev/null
