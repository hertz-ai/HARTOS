#!/bin/bash
set +e
W="/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/src/wayland.rs"
echo "===wayland.rs: is CompositorHandler/ShmHandler/BufferHandler/LayerShell present ANYWHERE?==="
grep -nE "CompositorHandler|impl.*ShmHandler|impl.*BufferHandler|WlrLayerShell|smithay::delegate|delegate_dispatch|on_commit_buffer_handler" "$W"
echo ""
echo "===wayland.rs head: module doc says which handlers are in-scope (lines 50-110 imports)==="
sed -n '60,110p' "$W"
echo ""
echo "=== ROADMAP M7 / Milestone 7 / DRM scope ==="
R="/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/ROADMAP.md"
ls -la "$R" 2>/dev/null
grep -niE "M7|milestone 7|DRM|udev|real.hardware|run_udev|buildFeatures" "$R" 2>/dev/null | head -40
