#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# Build a MODERN XWayland (23.2.6) on Ubuntu 22.04 so HART-comp's M3 XWayland
# windows actually PAINT in this dev box.
# ════════════════════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
#   Ubuntu 22.04 (jammy) ships ONLY XWayland 22.1.1. That version predates the
#   `xwayland_shell_v1` association protocol (added in XWayland 23.1). With 22.1.1,
#   XWayland associates an X11 window to its wl_surface via the LEGACY
#   `WL_SURFACE_ID` client message — and Smithay (pinned rev 4784339) does NOT
#   complete that legacy association for an external compositor: its `WL_SURFACE_ID`
#   handler only stores the id; `set_wl_surface` is `pub(crate)` and is driven solely
#   by the modern `xwayland_shell` / `WL_SURFACE_SERIAL` path. Result on 22.1.1:
#   `X11Surface::wl_surface()` stays `None`, so `X11Surface::render_elements`
#   returns an empty Vec, so the X11 window MAPS BUT NEVER PAINTS.
#
#   XWayland 23.2 speaks `xwayland_shell_v1`, which Smithay DOES handle (commit hook
#   → `surface_for_serial` → `set_wl_surface`). So the X11 surface associates and
#   paints with ZERO change to the association code — HART-comp's winit backend only
#   needs the newer X server on PATH (Smithay spawns it via `Command::new("Xwayland")`
#   + a PATH lookup, preserving PATH across the env it clears).
#
#   In PRODUCTION (the NixOS ISO) XWayland is already 23.1+, so this build is a
#   DEV-BOX bring-up step only — it is NOT part of the shipped OS.
#
# WHAT IT DOES
#   1. Builds libwayland 1.22 from source (XWayland 23.2 needs wayland-client >= 1.21;
#      jammy ships 1.20) into /opt/wayland-new, and bakes its rpath into Xwayland.
#   2. Installs the header-only newer xorgproto (presentproto >= 1.3 + xwaylandproto)
#      and wayland-protocols >= 1.30 (both arch:all, no glibc bump) from the noble pool
#      on archive.ubuntu.com (the only reachable mirror on this firewalled box).
#   3. Builds standalone XWayland 23.2.6 from the noble source tarball, linking the
#      jammy glibc 2.35 (a prebuilt noble binary needs glibc 2.38 — won't run here).
#   4. Installs the result to /opt/xwayland-new/Xwayland.
#
#   Put /opt/xwayland-new first on PATH when launching hart-comp (the m3_run.sh /
#   m3_verify_x11.sh harnesses already do). Verify with:
#     /opt/xwayland-new/Xwayland -version   # → 23.2.6
#     strings /opt/xwayland-new/Xwayland | grep xwayland_shell_v1
#
# Idempotent-ish: re-running rebuilds from the cached source trees under /root.
set -u
export DEBIAN_FRONTEND=noninteractive
WLNEW=/opt/xwayland-new ; WLLIB=/opt/wayland-new
WLB=/root/wl-build ; XWB=/root/xw-build
POOL_W=http://archive.ubuntu.com/ubuntu/pool/main/w/wayland
POOL_XW=http://archive.ubuntu.com/ubuntu/pool/main/x/xwayland
POOL_PROTO=http://archive.ubuntu.com/ubuntu/pool/main/x/xorgproto
POOL_WP=http://archive.ubuntu.com/ubuntu/pool/main/w/wayland-protocols

echo "── [0/4] enable universe + base build tools ─────────────────────────────"
add-apt-repository -y universe 2>&1 | tail -1
apt-get update -qq 2>&1 | tail -1
apt-get install -y -qq meson ninja-build gcc pkg-config \
  libffi-dev libexpat1-dev libxml2-dev \
  libxfont-dev libxkbfile-dev libxcvt-dev libxshmfence-dev libpixman-1-dev \
  libdrm-dev libepoxy-dev libgbm-dev x11proto-dev xtrans-dev libbsd-dev nettle-dev \
  libtirpc-dev libxau-dev libxdmcp-dev xutils-dev libxext-dev \
  mesa-common-dev libgl-dev libglx-dev libegl-dev 2>&1 | tail -2

echo "── [1/4] libwayland 1.22 from source → $WLLIB ───────────────────────────"
mkdir -p "$WLB"; cd "$WLB" || exit 2
ORIG=wayland_1.22.0.orig.tar.gz
[ -s "$ORIG" ] || curl -s -o "$ORIG" "$POOL_W/$ORIG"
rm -rf src; mkdir src; tar xf "$ORIG" -C src --strip-components=1
cd src && rm -rf build
meson setup build -Dprefix="$WLLIB" -Ddocumentation=false -Dtests=false -Ddtd_validation=false 2>&1 | tail -3
ninja -C build 2>&1 | tail -2 && ninja -C build install 2>&1 | tail -2

echo "── [2/4] newer xorgproto + wayland-protocols (arch:all, header-only) ─────"
cd /tmp
PROTO_DEB=$(curl -s "$POOL_PROTO/" | grep -oE 'x11proto-dev_[0-9][^"]*_all\.deb' | sort -V | tail -1)
curl -s -o "$PROTO_DEB" "$POOL_PROTO/$PROTO_DEB" && dpkg -i "$PROTO_DEB" 2>&1 | tail -1
WP_DEB=$(curl -s "$POOL_WP/" | grep -oE 'wayland-protocols_1\.(3[0-9]|4[0-9])[^"]*_all\.deb' | sort -V | tail -1)
curl -s -o "$WP_DEB" "$POOL_WP/$WP_DEB" && dpkg -i "$WP_DEB" 2>&1 | tail -1
echo "  presentproto=$(pkg-config --modversion presentproto) wayland-protocols=$(pkg-config --modversion wayland-protocols) xwaylandproto=$(pkg-config --modversion xwaylandproto)"

echo "── [3/4] XWayland 23.2.6 from source (links jammy glibc) → $WLNEW ────────"
mkdir -p "$XWB"; cd "$XWB" || exit 3
XORIG=xwayland_23.2.6.orig.tar.xz
[ -s "$XORIG" ] || curl -s -o "$XORIG" "$POOL_XW/$XORIG"
rm -rf src; mkdir src; tar xf "$XORIG" -C src --strip-components=1
cd src && rm -rf build
export PATH="$WLLIB/bin:$PATH"   # new wayland-scanner (1.22) first
export PKG_CONFIG_PATH="$WLLIB/lib/x86_64-linux-gnu/pkgconfig:$WLLIB/lib/pkgconfig:$WLLIB/share/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig:/usr/share/pkgconfig"
export LDFLAGS="-Wl,-rpath,$WLLIB/lib/x86_64-linux-gnu ${LDFLAGS:-}"   # find libwayland 1.22 at runtime
meson setup build -Dprefix="$WLNEW" \
  -Dxvfb=false -Dxwayland_eglstream=false -Dxwayland_ei=false \
  -Ddocs=false -Ddevel-docs=false -Ddocs-pdf=false -Dlibunwind=false -Dlibdecor=false \
  -Ddri3=true -Dglamor=true -Ddrm=true \
  -Dxkb_dir=/usr/share/X11/xkb -Dxkb_output_dir=/var/lib/xkb 2>&1 | tail -3
ninja -C build 2>&1 | tail -2
BIN=$(find build -name 'Xwayland' -type f | head -1)
mkdir -p "$WLNEW"; cp "$BIN" "$WLNEW/Xwayland"; chmod 755 "$WLNEW/Xwayland"
chmod -R a+rX "$WLNEW" "$WLLIB"

echo "── [4/4] verify ─────────────────────────────────────────────────────────"
"$WLNEW/Xwayland" -version 2>&1 | head -1
ldd "$WLNEW/Xwayland" 2>&1 | grep -i "not found" && echo "  !! missing libs" || echo "  all libs resolved"
strings "$WLNEW/Xwayland" | grep -m1 xwayland_shell_v1 && echo "  ✓ advertises xwayland_shell_v1"
echo "DONE — put $WLNEW first on PATH when launching hart-comp."
