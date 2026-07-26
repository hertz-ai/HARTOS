#!/usr/bin/env bash
# M6 alt harness — run hart-comp DIRECTLY on WSLg's wayland-0 (not nested in headless
# sway). The M1-M5 nesting existed only because hart-comp had no screencopy, so grim
# had to capture the HOST. M6 adds screencopy, so we can grim hart-comp DIRECTLY via
# $HART_SOCK regardless of the host — which lets us skip the unstable headless-sway
# nest (its nested EGL window surface goes ContextLost ~3s in under WSL). WSLg's
# wayland-0 has a stable EGL surface, so the framebuffer keeps updating and the
# killswitch-black / crossfade / cursor draws are observable across multiple captures.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6w-hart.log
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m6_artifacts
mkdir -p "$OUTDIR"
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
sleep 1.0
rm -f "$IPC_SOCK" 2>/dev/null || true
find "$RUNTIME" -maxdepth 1 -name 'wayland-[3-9]*' -delete 2>/dev/null || true
: > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true

# WSLg's wayland-0 is the real host. Run hart-comp nested in IT (stable EGL).
echo "[m6w] launching hart-comp on WSLg wayland-0 ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=wayland-0
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1
  exec '$HART_BIN' >> '$HART_LOG' 2>&1
" &
HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25
done
[ -z "$HART_SOCK" ] && { echo "FAIL hart"; tail -30 "$HART_LOG"; exit 1; }
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[m6w] hart $HART_SOCK ipc=$([ -S "$IPC_SOCK" ] && echo up)"

GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/m6w-grim.err; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1.png" && echo "[m6w] -> $1.png ($(stat -c%s "$2"))"; }
LC() { local l="$1"; shift; setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland NO_AT_BRIDGE=1 "$@" >"/tmp/m6w-$l.log" 2>&1 & }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" "$@"; }

LC calc gnome-calculator; sleep 1.6
LC foot foot; sleep 1.8

echo "── A windows ──"; GD /tmp/m6w-A.png; SV A2-windows /tmp/m6w-A.png
echo "── B killswitch ON ──"; IPC screen.kill on; sleep 0.5; GD /tmp/m6w-B.png; SV B2-killswitch-on /tmp/m6w-B.png
echo "── C killswitch OFF ──"; IPC screen.kill off; sleep 0.6; GD /tmp/m6w-C.png; SV C2-killswitch-off /tmp/m6w-C.png
echo "── D crossfade (capture a few times across the fade) ──"
IPC workspace.switch 2 >/dev/null; sleep 0.5; IPC workspace.switch 1 >/dev/null
GD /tmp/m6w-D0.png; SV D2-crossfade-t0 /tmp/m6w-D0.png
sleep 0.06; GD /tmp/m6w-D1.png; SV D2-crossfade-t1 /tmp/m6w-D1.png
sleep 0.5; GD /tmp/m6w-D2.png; SV D2-crossfade-settled /tmp/m6w-D2.png

echo ""
echo "=== luminance (A normal, B black-or-refused, C back, D fade frames) ==="
runuser -u sathish -- python3 /tmp/m6_lum.py /tmp/m6w-A.png /tmp/m6w-B.png /tmp/m6w-C.png /tmp/m6w-D0.png /tmp/m6w-D1.png /tmp/m6w-D2.png

echo ""
echo "=== hart M6 log (last 25 relevant) ==="
strip_ansi <"$HART_LOG" | grep -iE 'screencopy|screen.kill|capture/input|workspace.switch|window.opened|ContextLost|submit' | tail -25
echo "=== DONE ==="
