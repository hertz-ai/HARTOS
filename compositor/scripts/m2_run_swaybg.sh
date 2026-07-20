#!/usr/bin/env bash
# Inner swaybg launcher — runs AS sathish (no further nesting). $1 = hart-comp sock.
# Kept as its own file so the '#rrggbb' color never passes through a `bash -c "..."`
# layer (where the leading # would start a comment / be eaten by the outer shell).
set -u
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY="${1:-wayland-2}"
exec swaybg --color '#1a6ef5' --mode solid_color
