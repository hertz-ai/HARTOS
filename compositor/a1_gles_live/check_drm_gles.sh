#!/usr/bin/env bash
# A1 - compile-verify the DRM/udev GLES path (src/udev.rs build_gles_renderer + the
# GlesRenderer/EGLDisplay/EGLContext wiring). This is the path that CANNOT run live in
# WSL (GBM eglInitialize fails - no /dev/dri render node), so the proof it is real code
# is that it COMPILES under --features smithay (the real-HW DRM backend feature set).
set -u
export PATH=$HOME/.cargo/bin:/usr/bin:/bin:$PATH
COMP=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor
cd "$COMP" || exit 2
echo "=== cargo build --features smithay (DRM/udev + GLES, real-HW backend) ==="
date +%T
cargo build --features smithay 2>&1 | tail -25
echo "EXIT=${PIPESTATUS[0]}"
date +%T
ls -la target/debug/hart-comp 2>/dev/null
