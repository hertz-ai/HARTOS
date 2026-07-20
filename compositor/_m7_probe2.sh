#!/bin/bash
set +e
REPO="/mnt/c/Users/sathi/PycharmProjects/HARTOS"
cd "$REPO" || exit 1
echo "===0a68414 forward-port commit body (smithay-green claim)==="
git show -s --format='%H%n%s%n%n%b' 0a68414 2>/dev/null | head -40
echo ""
echo "===does wayland.rs ever get a run_udev / event loop / DRM RUN path today?==="
grep -nE "fn run_udev|fn run_drm|fn run|EventLoop::try_new|LibSeatSession|UdevBackend|LibinputInputBackend|insert_source|DrmCompositor|GbmAllocator|primary_gpu" compositor/src/wayland.rs 2>/dev/null
echo ""
echo "===wayland.rs total lines + last 40 lines (does it END at handlers, no run path?)==="
wc -l compositor/src/wayland.rs
echo "--- tail ---"
tail -45 compositor/src/wayland.rs
