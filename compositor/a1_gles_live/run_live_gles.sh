#!/usr/bin/env bash
# A1 - LIVE Tier-1 GPU compositor test (winit/WSLg GLES path).
#
# Runs hart-comp (--features winit) nested in WSLg's wayland-0 with the GlesRenderer
# on the REAL GPU (D3D12 -> Intel UHD via WSL2 passthrough, NO software-GL force),
# spawns a Wayland client so a real window maps + paints, captures the compositor's
# OWN framebuffer via wlr-screencopy (grim on hart-comp's socket), and measures the
# live paint cadence (HART_COMP_FPS). This is the runnable twin of the DRM/udev GLES
# path (src/udev.rs build_gles_renderer), which cannot run live in WSL because the
# GBM EGL platform fails eglInitialize here (no /dev/dri render node).
set -u
export PATH=$HOME/.cargo/bin:/usr/bin:/bin:/usr/local/bin:$PATH
# When `wsl.exe -- bash <script>` runs non-interactively the systemd user session may
# not start, so /run/user/1000/wayland-0 (the symlink into WSLg) is absent. The WSLg
# host socket ALWAYS exists at /mnt/wslg/runtime-dir/wayland-0, and that dir is writable
# (sathish), so use it for BOTH the host connection AND hart-comp's own socket.
if [ -S /run/user/1000/wayland-0 ]; then export XDG_RUNTIME_DIR=/run/user/1000
else export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir; fi
echo "[env] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR  host wayland-0: $(ls -la $XDG_RUNTIME_DIR/wayland-0 2>&1 | awk '{print $1}')"
COMP=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor
OUT="$COMP/a1_gles_live"
LOG=/tmp/a1-hart.log
mkdir -p "$OUT"
cd "$COMP" || exit 2

echo "=================== A1 LIVE GLES (winit/WSLg) ==================="
echo "[host GPU] $(glxinfo 2>/dev/null | grep -i 'OpenGL renderer' | head -1)"

# (1) Build (incremental; picks up the HART_COMP_FPS probe edit) -----------------
echo "[build] cargo build --features winit ..."
cargo build --features winit 2>&1 | tail -4
BIN="$COMP/target/debug/hart-comp"
[ -x "$BIN" ] || { echo "FAIL: no binary at $BIN"; exit 3; }
echo "[build] binary: $(ls -la "$BIN" | awk '{print $5, $9}')"

# (2) Clean any prior run, launch nested in WSLg wayland-0 -----------------------
pkill -9 -f target/debug/hart-comp 2>/dev/null
pkill -9 foot 2>/dev/null
pkill -9 weston-simple-egl 2>/dev/null
sleep 0.6
find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-[3-9]*' -delete 2>/dev/null
: > "$LOG"

# REAL GPU (no LIBGL_ALWAYS_SOFTWARE). Ask smithay's GLES renderer to log GL_RENDERER
# (gles=debug) so the captured renderer string proves hardware (D3D12 Intel) vs llvmpipe.
HART_COMP_FPS=1 NO_COLOR=1 \
  RUST_LOG="info,smithay::backend::renderer::gles=debug,smithay::backend::egl=debug" \
  WAYLAND_DISPLAY=wayland-0 \
  setsid "$BIN" >>"$LOG" 2>&1 &
HPID=$!

# wait for hart-comp's own socket to appear
SOCK=""
for i in $(seq 1 100); do
  SOCK=$(grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' "$LOG" 2>/dev/null \
         | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$SOCK" ] && [ -S "$XDG_RUNTIME_DIR/$SOCK" ] && break
  kill -0 "$HPID" 2>/dev/null || { echo "FAIL: hart-comp exited early"; break; }
  sleep 0.25
done
echo "[run] hart-comp pid=$HPID socket=${SOCK:-NONE}"
if [ -z "$SOCK" ]; then
  echo "=== last 40 log lines (startup failure) ==="; tail -40 "$LOG"; kill "$HPID" 2>/dev/null; exit 4
fi

# (3) Spawn an extra real client so the frame shows window content (built-in also tries foot)
WAYLAND_DISPLAY="$SOCK" setsid foot >/tmp/a1-foot.log 2>&1 &
sleep 2.5   # let it map + paint + the FPS probe log a couple of 1s ticks

# (4) Capture hart-comp's OWN framebuffer (wlr-screencopy) - PNG to view, PPM to analyse
WAYLAND_DISPLAY="$SOCK" grim -t png "$OUT/a1_winit_gles_frame.png" 2>/tmp/a1-grim.err
echo "[grim png] exit=$? -> $(ls -la "$OUT/a1_winit_gles_frame.png" 2>/dev/null | awk '{print $5}') bytes"
WAYLAND_DISPLAY="$SOCK" grim -t ppm "$OUT/a1_winit_gles_frame.ppm" 2>>/tmp/a1-grim.err
# host-side cross check (what WSLg sees)
WAYLAND_DISPLAY=wayland-0 grim -t png "$OUT/a1_host_view.png" 2>/dev/null

# (5) Pixel analysis: prove it is REAL composited pixels, not a blank scanout ----
python3 - "$OUT/a1_winit_gles_frame.ppm" <<'PY'
import sys
from collections import Counter
p=sys.argv[1]
try:
    d=open(p,'rb').read()
except Exception as e:
    print("PPM read failed:", e); sys.exit(0)
# parse P6 header: magic, w, h, maxval
toks=[]; i=0
while len(toks)<4:
    while i<len(d) and d[i] in b' \t\r\n': i+=1
    s=i
    while i<len(d) and d[i] not in b' \t\r\n': i+=1
    toks.append(d[s:i])
i+=1
mag,w,h,mx=toks[0],int(toks[1]),int(toks[2]),int(toks[3])
px=d[i:]
n=w*h
c=Counter()
lum=0.0
for j in range(0,min(len(px),n*3),3):
    r,g,b=px[j],px[j+1],px[j+2]
    c[(r,g,b)]+=1
    lum+=0.2126*r+0.7152*g+0.0722*b
print(f"resolution={w}x{h} distinct_colors={len(c)} mean_luma={lum/n:.1f}")
print("top colors (rgb:count):", ", ".join(f"{k}:{v}" for k,v in c.most_common(6)))
nonblack=sum(v for k,v in c.items() if k!=(0,0,0))
print(f"non-black_pixels={nonblack} ({100.0*nonblack/n:.1f}% of frame)")
PY

# (6) Timing / renderer evidence ------------------------------------------------
echo "=== measured paint cadence (HART_COMP_FPS) ==="
grep -a "measured paint cadence" "$LOG" | tail -6
echo "=== GLES renderer / EGL init evidence ==="
grep -aiE "GL_RENDERER|GL_VENDOR|GL_VERSION|renderer:|EGLDisplay|EGL .*version|Initialized GLES|GlesRenderer|gl_renderer" "$LOG" | head -12
echo "=== compositor lifecycle markers ==="
grep -aiE "listening on its own|compositor initialized|spawned test client|render error|failed to submit" "$LOG" | head -12

# (7) Persist the live log into the evidence dir (tmpfs /tmp is wiped on WSL restart),
#     trimmed to the meaningful markers so the evidence is self-contained.
grep -aE 'render path|Initializing a winit|EGL Version|is supported|listening on its own|spawned test client|compositor initialized|measured paint cadence|ContextLost|render error|failed to submit' "$LOG" > "$OUT/a1_live_run.log" 2>/dev/null
echo "[evidence] a1_live_run.log = $(wc -l < "$OUT/a1_live_run.log") lines"

# (8) Cleanup -------------------------------------------------------------------
kill "$HPID" 2>/dev/null
pkill -9 foot 2>/dev/null
pkill -9 -f target/debug/hart-comp 2>/dev/null
echo "=== A1 LIVE GLES DONE ==="
