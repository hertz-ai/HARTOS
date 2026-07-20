#!/usr/bin/env bash
# M6 FADE live proof — with HART_COMP_DEBUG_FADE=1, the render loop logs the actual
# sub-1.0 alpha it hands the renderer whenever a window is mid-map-fade or a workspace
# crossfade is in flight. This proves the fade MATH runs live (the alpha ramps 0->1)
# even though the nested WSL EGL surface freezes before a mid-fade frame can be grabbed
# by grim — the alpha is computed and applied every frame regardless of whether the
# host `submit` succeeds.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6f-hart.log
SWAY_LOG=/tmp/m6f-sway.log
SWAY_CFG=/tmp/m6f-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
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
HOST=""
for i in $(seq 1 60); do HOST=$(strip_ansi <"$SWAY_LOG" | grep -oE "wayland-[0-9]+" | tail -1); [ -n "$HOST" ] && [ -S "$RUNTIME/$HOST" ] && break; sleep 0.25; done
echo "host=$HOST"
# DEBUG_FADE on so the alpha ramp is logged.
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 HART_COMP_DEBUG_FADE=1 NO_COLOR=1; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART=""
for i in $(seq 1 60); do HART=$(strip_ansi <"$HART_LOG" | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART" ] && [ -S "$RUNTIME/$HART" ] && break; sleep 0.25; done
echo "hart=$HART"
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done

# foot map → map-fade; then ws switch → crossfade.
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART" foot >/tmp/m6f-foot.log 2>&1 &
sleep 1.0
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" workspace.switch 2 >/dev/null 2>&1 || true
sleep 0.3
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/m6_ipc.py "$IPC_SOCK" workspace.switch 1 >/dev/null 2>&1 || true
sleep 0.3

echo ""
echo "=== effect.fade log lines (LIVE: alpha ramps toward 1.0) ==="
strip_ansi <"$HART_LOG" | grep "effect.fade" | head -30
echo "=== effect.fade count ==="
strip_ansi <"$HART_LOG" | grep -c "effect.fade"
echo "DONE-FADE"
