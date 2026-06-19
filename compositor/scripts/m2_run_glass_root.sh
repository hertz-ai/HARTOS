#!/usr/bin/env bash
# Root-direct launcher for the GTK4 WebKit glass host (the harness reliably keeps
# foreground/auto-backgrounded ROOT wayland clients alive; runuser-sathish ones get
# SIGTERM'd). HART-comp's socket is srwxr-xr-x so root (as "other") can connect.
# $1 = hart-comp wayland socket.
set -u
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY="${1:-wayland-2}"
export GI_TYPELIB_PATH=/usr/local/lib/x86_64-linux-gnu/girepository-1.0:${GI_TYPELIB_PATH:-}
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
# gtk4-layer-shell MUST be preloaded ahead of libwayland-client (linking.md) or
# gtk_layer_init_for_window() no-ops ("not a layer surface").
export LD_PRELOAD=/usr/local/lib/x86_64-linux-gnu/libgtk4-layer-shell.so${LD_PRELOAD:+:$LD_PRELOAD}
# WebKitGTK Web/Network-process sandbox can't set up bwrap mounts here → disable.
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
export WEBKIT_FORCE_SANDBOX=0
# Software-GL paint floor (the cage/sway/hart-comp contract).
export WEBKIT_DISABLE_DMABUF_RENDERER=1
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export LIBGL_ALWAYS_SOFTWARE=1
export GDK_BACKEND=wayland
export XDG_DATA_DIRS=/usr/local/share:/usr/share
export HART_REPO=/mnt/c/Users/sathi/PycharmProjects/HARTOS
export HART_LIQUID_PORT="${HART_LIQUID_PORT:-6800}"
export HART_DATA_DIR=/tmp/m2-hart-data
export HART_HOST_DISPLAY="${HART_HOST_DISPLAY:-wayland-1}"
export HART_SHOT_PATH="${HART_SHOT_PATH:-/tmp/m2-glass-shell.png}"
export HART_GLASS_STATUS="${HART_GLASS_STATUS:-/tmp/m2-glass-status.log}"
export HART_SHOT_DELAY_MS="${HART_SHOT_DELAY_MS:-4500}"
mkdir -p "$HART_DATA_DIR"
exec python3 -u /tmp/m2_glass_host.py
