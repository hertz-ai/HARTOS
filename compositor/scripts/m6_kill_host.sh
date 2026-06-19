#!/usr/bin/env bash
# M6 killswitch + crossfade proof via the HOST grim (within the ~3s nested-EGL window).
# The kill-switch REFUSES direct screencopy (proven separately: grim on $HART_SOCK gets
# a `failed` frame), so the BLACK SURFACE itself is observed via the HOST, which
# composites hart-comp and is NOT gated by hart-comp's killswitch. Fires fast: boot →
# 1 app → host-grim baseline → kill on → host-grim BLACK → kill off → host-grim back.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6k-hart.log
SWAY_LOG=/tmp/m6k-sway.log
SWAY_CFG=/tmp/m6k-sway.config
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
echo "[m6k] host $HOST_SOCK"

setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART_SOCK=""
for i in $(seq 1 60); do HART_SOCK=$(strip_ansi <"$HART_LOG" | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25; done
[ -z "$HART_SOCK" ] && { echo FAILhart; tail -20 "$HART_LOG"; exit 1; }
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
echo "[m6k] hart $HART_SOCK ipc=$([ -S "$IPC_SOCK" ] && echo up)"

GH() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" grim "$1" 2>>/tmp/m6k-grim.err; }
GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/m6k-grimd.err; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1.png" && echo "[m6k] -> $1.png ($(stat -c%s "$2"))"; }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" "$@"; }

# ONE app, minimal settle — then fire the whole sequence FAST (target < 3s post-boot).
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot >/tmp/m6k-foot.log 2>&1 &
sleep 1.3

echo "── baseline (host sees hart-comp + window) ──"; GH /tmp/m6k-base.png; SV K0-baseline-host /tmp/m6k-base.png
echo "── direct grim BEFORE kill (should succeed) ──"; GD /tmp/m6k-d-before.png; SV K0b-direct-before /tmp/m6k-d-before.png
echo "── KILL ON ──"; IPC screen.kill on
echo "── host grim WHILE killed → hart-comp draws BLACK over everything ──"; GH /tmp/m6k-black.png; SV K1-killswitch-BLACK-host /tmp/m6k-black.png
echo "── direct grim WHILE killed → REFUSED (expect unreadable/empty) ──"; GD /tmp/m6k-d-killed.png; SV K1b-direct-REFUSED /tmp/m6k-d-killed.png
echo "── KILL OFF ──"; IPC screen.kill off; sleep 0.3
echo "── host grim after restore → window back ──"; GH /tmp/m6k-back.png; SV K2-killswitch-off-host /tmp/m6k-back.png

echo ""
echo "=== luminance: baseline vs BLACK vs restored (host captures) ==="
runuser -u sathish -- python3 /tmp/m6_lum.py /tmp/m6k-base.png /tmp/m6k-black.png /tmp/m6k-back.png
echo "=== direct-grim sizes (before=real, killed=refused→tiny/absent) ==="
for f in /tmp/m6k-d-before.png /tmp/m6k-d-killed.png; do printf "  %s: " "$(basename $f)"; [ -f "$f" ] && stat -c%s "$f" || echo MISSING; echo; done

echo ""
echo "=== hart M6 log ==="
strip_ansi <"$HART_LOG" | grep -iE 'screen.kill|screencopy|capture/input|window.opened|ContextLost' | tail -15
echo "=== DONE ==="
