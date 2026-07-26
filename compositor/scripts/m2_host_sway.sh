#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M2 — Host-2: a headless sway 1.7 on llvmpipe as the Wayland host.
# ════════════════════════════════════════════════════════════════════════════
# Runs as the NON-ROOT user `sathish` (uid 1000) — WLR refuses to run as root and
# the user-session XDG_RUNTIME_DIR (/run/user/1000) already exists. WSLg's broken
# systemd user session means we cannot rely on wayland-0, so we bring up our OWN
# nested host compositor (headless sway, software renderer) that DOES advertise a
# usable wl_compositor + wl_seat for hart-comp's winit backend to nest in.
#
# Headless sway needs NO DRM and NO real GPU: WLR_BACKENDS=headless + the pixman
# software renderer. Xwayland is absent on this box (non-fatal: we set
# `xwayland disable`). The host exposes a fresh wayland-N socket under
# /run/user/1000; we echo that socket so the caller can nest hart-comp in it.
set -u

export XDG_RUNTIME_DIR=/run/user/1000
export WLR_BACKENDS=headless
export WLR_RENDERER=pixman
export WLR_RENDERER_ALLOW_SOFTWARE=1
export LIBGL_ALWAYS_SOFTWARE=1
# A predictable socket name so the caller can point hart-comp at it.
export WAYLAND_DISPLAY=wayland-host
# Headless output size (the virtual screen the nested hart-comp window lives on).
export WLR_HEADLESS_OUTPUTS=1

LOG=/tmp/m2-sway-host.log
CFG=/tmp/m2-sway.config
: >"$LOG"

# Minimal sway config: a headless output at a fixed mode, Xwayland off, no bars,
# and a `swaymsg exit`-able session. We do NOT autostart anything here — the
# caller launches hart-comp into this host explicitly.
cat >"$CFG" <<'EOF'
# headless sway host for HART-comp nesting (M2)
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
EOF

echo "[m2_host_sway] starting headless sway (WAYLAND_DISPLAY=$WAYLAND_DISPLAY) ..." | tee -a "$LOG"
exec sway -c "$CFG" -d 2>>"$LOG"
