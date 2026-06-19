#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# HART-comp M6 — effects + cage/sway parity:
#   (1) SCREENCOPY (headline): zwlr_screencopy_v1 served against HART-comp's OWN
#       framebuffer, so `WAYLAND_DISPLAY=$HART_SOCK grim direct.png` captures
#       HART-comp DIRECTLY (M1-M5 had to grim the SWAY HOST recomposite).
#   (2) CURSOR: a software cursor renders at the pointer each frame (default arrow),
#       captured IN hart-comp's framebuffer (the host's cursor is NOT in it).
#   (3) ANIMATIONS: per-window fade-in on map + a workspace-switch crossfade.
#   (4) SCREEN KILL-SWITCH: IPC `screen.kill {on}` → full-output black + no input;
#       `grim` then shows an all-black frame; off → windows return.
#
# Topology (proven M1-M5):
#   headless sway 1.7 (non-root sathish, WLR_BACKENDS=headless, pixman)   ← HOST
#     └── hart-comp (winit, nested, OWN wayland-N socket + hart-comp.sock IPC)
#           ├── foot              (xdg-shell terminal)
#           └── gnome-calculator  (GTK4 xdg-shell)
#
# Captures land in compositor/m6_artifacts/. The DIRECT ones (grim against
# $HART_SOCK) are the screencopy proof; the HOST ones are kept only as a sanity
# cross-check that the two agree (they should, modulo the host's own cursor).
set -u

RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/m6-hartcomp.log
SWAY_LOG=/tmp/m6-sway-host.log
SWAY_CFG=/tmp/m6-sway.config
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/m6_artifacts
mkdir -p "$OUTDIR"

strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

# ── 0. Clean slate ──
pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/m6" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
sleep 1.0
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" "$HART_LOG" "$SWAY_LOG" 2>/dev/null || true

cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"
chmod 755 "$HART_BIN"; chown sathish:sathish "$HART_BIN" 2>/dev/null || true

# ── 1. Host: headless sway ──
cat >"$SWAY_CFG" <<'EOF'
output HEADLESS-1 mode 1280x800 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF
echo "[m6] starting headless sway host ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1
  exec sway -c '$SWAY_CFG' -d > '$SWAY_LOG' 2>&1
" &
HOST_SOCK=""
for i in $(seq 1 60); do
  HOST_SOCK=$(strip_ansi <"$SWAY_LOG" 2>/dev/null | grep -oE "wayland-[0-9]+" | tail -1)
  [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break
  sleep 0.25
done
[ -z "$HOST_SOCK" ] && { echo "FAILED: host sway socket"; tail -30 "$SWAY_LOG"; exit 1; }
echo "[m6] host sway up (socket: $HOST_SOCK)"

# ── 2. Nest hart-comp ──
echo "[m6] launching hart-comp nested ..."
setsid runuser -u sathish -- bash -c "
  export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK
  export WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1
  export HART_COMP_NO_TEST_CLIENT=1 NO_COLOR=1
  exec '$HART_BIN' --force-software > '$HART_LOG' 2>&1
" &
HART_SOCK=""
for i in $(seq 1 60); do
  HART_SOCK=$(strip_ansi <"$HART_LOG" 2>/dev/null \
              | grep -oE 'listening on its own wayland socket.*wayland-[0-9]+' \
              | grep -oE 'wayland-[0-9]+' | tail -1)
  [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break
  sleep 0.25
done
[ -z "$HART_SOCK" ] && { echo "FAILED: hart-comp socket"; tail -40 "$HART_LOG"; exit 1; }
echo "[m6] hart-comp OWN socket: $HART_SOCK"
for i in $(seq 1 40); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
[ -S "$IPC_SOCK" ] && echo "[m6] IPC socket up: $IPC_SOCK" || { echo "FAILED: hart-comp.sock"; exit 1; }

# Confirm hart-comp advertises the screencopy global to a client.
sleep 0.5
echo "[m6] checking wayland-info for zwlr_screencopy on \$HART_SOCK ..."
runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" \
  wayland-info 2>/dev/null | grep -i screencopy && echo "[m6] ✓ zwlr_screencopy_manager_v1 advertised" \
  || echo "[m6] (wayland-info unavailable or global not listed — grim is the real test below)"

# helpers
grim_direct() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" \
    grim "$1" 2>/tmp/m6-grim-direct.err; }
grim_host() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" \
    grim "$1" 2>/tmp/m6-grim-host.err; }
save() { local name="$1" src="$2"; [ -f "$src" ] && cp "$src" "$OUTDIR/$name.png" \
    && echo "[m6] -> $OUTDIR/$name.png ($(stat -c%s "$src") bytes)"; }
launch_client() { local label="$1"; shift
  setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" \
    WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland NO_AT_BRIDGE=1 \
    "$@" >"/tmp/m6-$label.log" 2>&1 &
  echo "[m6] launched $label"; }
ipc() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 \
    /mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/scripts/m6_ipc.py "$IPC_SOCK" "$@"; }
# Warp the host cursor (sway forwards pointer motion to its fullscreen surface =
# hart-comp, which then renders ITS software cursor at that location).
warp() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME swaymsg -s "$RUNTIME/$HOST_SOCK" \
    "seat - cursor set $1 $2" >/dev/null 2>&1 || \
    runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HOST_SOCK" swaymsg \
    "seat seat0 cursor set $1 $2" >/dev/null 2>&1 || true; }

echo ""
echo "════════════ M6 PROOF ════════════"

# ── (1) SCREENCOPY — capture hart-comp DIRECTLY with nothing mapped yet (the splash
#       + default cursor at 0,0). This is the headline: grim hits hart-comp, not sway. ──
echo ""
echo "── (1) screencopy: grim DIRECT against hart-comp (\$HART_SOCK) ──"
grim_direct /tmp/m6-01-direct-empty.png && save 01-direct-empty /tmp/m6-01-direct-empty.png \
  || { echo "FAILED: direct grim returned nothing"; cat /tmp/m6-grim-direct.err; }

# ── Launch two apps; capture mid-fade (within ~80ms of map) to catch the alpha ramp. ──
launch_client calc gnome-calculator
sleep 1.6
launch_client foot foot
# Grab IMMEDIATELY for the fade (foot maps ~now; the 150ms fade should be in progress).
grim_direct /tmp/m6-02-direct-fade.png && save 02-direct-fade-midmap /tmp/m6-02-direct-fade.png
sleep 1.2
# Settled capture — windows fully opaque, cursor visible.
grim_direct /tmp/m6-03-direct-windows.png && save 03-direct-two-windows /tmp/m6-03-direct-windows.png

# ── (2) CURSOR — warp the host cursor to two spots; each DIRECT capture should show
#       hart-comp's own arrow there (the host arrow is NOT in hart-comp's framebuffer). ──
echo ""
echo "── (2) cursor: warp + DIRECT capture (arrow is IN hart-comp's framebuffer) ──"
warp 200 160
sleep 0.4
grim_direct /tmp/m6-04-cursor-a.png && save 04-direct-cursor-200x160 /tmp/m6-04-cursor-a.png
warp 900 600
sleep 0.4
grim_direct /tmp/m6-05-cursor-b.png && save 05-direct-cursor-900x600 /tmp/m6-05-cursor-b.png

# ── (3) ANIMATIONS — workspace-switch crossfade. Switch to ws 2 (empty) then back to
#       ws 1; capture right after the switch-back to catch the whole-set crossfade. ──
echo ""
echo "── (3) animations: workspace-switch crossfade (ws1→ws2→ws1, capture mid-fade) ──"
ipc workspace.switch 2 >/dev/null 2>&1
sleep 0.5
ipc workspace.switch 1 >/dev/null 2>&1
grim_direct /tmp/m6-06-ws-crossfade.png && save 06-direct-ws-crossfade /tmp/m6-06-ws-crossfade.png
sleep 0.5

# ── (4) SCREEN KILL-SWITCH — IPC screen.kill on → DIRECT grim must be ALL BLACK; then
#       off → the windows return. This proves the black surface + the capture gate. ──
echo ""
echo "── (4) screen.kill ON → all-black DIRECT capture ──"
ipc screen.kill on
sleep 0.4
grim_direct /tmp/m6-07-killswitch-on.png && save 07-direct-killswitch-on /tmp/m6-07-killswitch-on.png
echo "── screen.kill OFF → windows return ──"
ipc screen.kill off
sleep 0.5
grim_direct /tmp/m6-08-killswitch-off.png && save 08-direct-killswitch-off /tmp/m6-08-killswitch-off.png

# Host cross-check (the host recomposite still works; kept as a sanity reference).
grim_host /tmp/m6-09-host-xcheck.png && save 09-host-xcheck /tmp/m6-09-host-xcheck.png

echo ""
echo "=== analysis: blackness of the killswitch capture vs a normal one ==="
runuser -u sathish -- python3 - <<'PY'
import struct, zlib, sys
def load(path):
    try:
        d=open(path,'rb').read()
    except Exception as e:
        return None
    if d[:8]!=b'\x89PNG\r\n\x1a\n': return None
    i=8; w=h=0; idat=b''; bd=ct=0
    while i<len(d):
        ln=struct.unpack('>I',d[i:i+4])[0]; typ=d[i+4:i+8]; chunk=d[i+8:i+8+ln]; i+=12+ln
        if typ==b'IHDR':
            w,h,bd,ct=struct.unpack('>IIBB',chunk[:10])
        elif typ==b'IDAT': idat+=chunk
        elif typ==b'IEND': break
    try: raw=zlib.decompress(idat)
    except Exception: return None
    ch={0:1,2:3,3:1,4:2,6:4}.get(ct,3)
    stride=w*ch
    # un-filter (paeth-capable minimal)
    out=bytearray(); prev=bytearray(stride); pos=0
    def paeth(a,b,c):
        p=a+b-c; pa=abs(p-a); pb=abs(p-b); pc=abs(p-c)
        return a if (pa<=pb and pa<=pc) else (b if pb<=pc else c)
    for y in range(h):
        f=raw[pos]; pos+=1; line=bytearray(raw[pos:pos+stride]); pos+=stride
        for x in range(stride):
            a=line[x-ch] if x>=ch else 0
            b=prev[x]; c=prev[x-ch] if x>=ch else 0
            v=line[x]
            if f==1: v=(v+a)&255
            elif f==2: v=(v+b)&255
            elif f==3: v=(v+((a+b)//2))&255
            elif f==4: v=(v+paeth(a,b,c))&255
            line[x]=v
        out+=line; prev=line
    return (w,h,ch,bytes(out))
def stats(path):
    r=load(path)
    if not r: print(f"  {path}: (unreadable)"); return None
    w,h,ch,px=r
    n=w*h
    # sample mean luminance + fraction of near-black pixels
    tot=0; black=0; step=max(1,(n)//20000)
    cnt=0
    for p in range(0,n,step):
        o=p*ch
        rr,gg,bb=px[o],px[o+1],px[o+2]
        lum=(rr*299+gg*587+bb*114)//1000
        tot+=lum; cnt+=1
        if lum<8: black+=1
    mean=tot/max(1,cnt); fb=black/max(1,cnt)
    print(f"  {path.split('/')[-1]:32} {w}x{h} mean_lum={mean:6.1f} frac_black={fb:.3f}")
    return mean,fb
import os
base="/tmp"
for f in ["m6-03-direct-windows.png","m6-07-killswitch-on.png","m6-08-killswitch-off.png"]:
    stats(os.path.join(base,f))
PY

echo ""
echo "=== hart-comp M6 log (screencopy / killswitch / cursor) ==="
strip_ansi <"$HART_LOG" | grep -iE 'screencopy|screen.kill|capture|cursor|workspace.switch|window.opened' | tail -25

echo ""
echo "=== M6 DONE — artifacts in $OUTDIR ==="
ls -la "$OUTDIR"/*.png 2>/dev/null
