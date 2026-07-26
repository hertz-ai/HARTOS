#!/usr/bin/env bash
# Supplementary verification in ONE fresh session: correct snap-zone name (left-half),
# cursor-in-frame proof, and a CLEAN killswitch on/off with grim exit codes.
set -u
RUNTIME=/run/user/1000
HART_BIN=/tmp/hart-comp-bin
HART_LOG=/tmp/supp-hart.log
SWAY_LOG=/tmp/supp-sway.log
SWAY_CFG=/tmp/supp-sway.config
GLASS_STATUS=/tmp/supp-glass-status.log
GLASS_LOG=/tmp/supp-glass.log
IPC_SOCK="$RUNTIME/hart-comp.sock"
OUTDIR=/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/integ_artifacts
strip_ansi() { sed -r 's/\x1b\[[0-9;]*m//g'; }

pkill -9 -f "/tmp/hart-comp-bin" 2>/dev/null || true
pkill -9 -f "integ_glass_host.py" 2>/dev/null || true
pkill -9 -u sathish -f "sway -c /tmp/supp" 2>/dev/null || true
pkill -9 -u sathish foot 2>/dev/null || true
pkill -9 -u sathish gnome-calculator 2>/dev/null || true
sleep 1.2
mkdir -p "$RUNTIME"; chown sathish:sathish "$RUNTIME"; chmod 700 "$RUNTIME"
find "$RUNTIME" -maxdepth 1 -name 'wayland-*' -delete 2>/dev/null || true
rm -f "$IPC_SOCK" "$GLASS_STATUS" 2>/dev/null || true
: > "$SWAY_LOG"; chown sathish:sathish "$SWAY_LOG"; : > "$HART_LOG"; chown sathish:sathish "$HART_LOG"
cp /root/hart-comp/target/debug/hart-comp "$HART_BIN"; chmod 755 "$HART_BIN"

cat >"$SWAY_CFG" <<EOF
output HEADLESS-1 mode 1440x900 position 0 0
xwayland disable
default_border none
focus_follows_mouse no
EOF
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 WLR_HEADLESS_OUTPUTS=1; exec sway -c '$SWAY_CFG' -d >> '$SWAY_LOG' 2>&1" &
HOST_SOCK=""
for i in $(seq 1 80); do HOST_SOCK=$(strip_ansi <"$SWAY_LOG" | grep -oE "wayland-[0-9]+" | tail -1); [ -n "$HOST_SOCK" ] && [ -S "$RUNTIME/$HOST_SOCK" ] && break; sleep 0.25; done
[ -z "$HOST_SOCK" ] && { echo "FAIL host"; exit 1; }
echo "[supp] HOST sway: $HOST_SOCK"
setsid runuser -u sathish -- bash -c "export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HOST_SOCK WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 HART_COMP_FORCE_SOFTWARE=1 HART_COMP_NO_TEST_CLIENT=1 HART_COMP_DEBUG_FADE=1 NO_COLOR=1 PATH=/opt/xwayland-new:\$PATH; exec '$HART_BIN' --force-software >> '$HART_LOG' 2>&1" &
HART_SOCK=""
for i in $(seq 1 80); do HART_SOCK=$(strip_ansi <"$HART_LOG" | grep -oE 'wayland-[0-9]+' | tail -1); [ -n "$HART_SOCK" ] && [ -S "$RUNTIME/$HART_SOCK" ] && break; sleep 0.25; done
[ -z "$HART_SOCK" ] && { echo "FAIL hart"; tail -20 "$HART_LOG"; exit 1; }
for i in $(seq 1 50); do [ -S "$IPC_SOCK" ] && break; sleep 0.2; done
chmod o+rwx "$RUNTIME/$HART_SOCK" "$IPC_SOCK" 2>/dev/null; chmod o+rx "$RUNTIME" 2>/dev/null
echo "[supp] HART-comp: $HART_SOCK  IPC: $([ -S "$IPC_SOCK" ] && echo UP || echo DOWN)"

GD() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" grim "$1" 2>>/tmp/supp-grim.err; echo "grim_rc=$?"; }
SV() { [ -f "$2" ] && cp "$2" "$OUTDIR/$1" && echo "[supp] saved $1 ($(stat -c%s "$2") bytes)"; }
IPC() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" "$@"; }
IPC6() { runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/m6_ipc.py "$IPC_SOCK" "$@"; }

# Glass shell (BACKGROUND) so the snap shot shows the desktop backdrop
GLASS_ENV="export XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY=$HART_SOCK GI_TYPELIB_PATH=/usr/local/lib/x86_64-linux-gnu/girepository-1.0 LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu LD_PRELOAD=/usr/local/lib/x86_64-linux-gnu/libgtk4-layer-shell.so WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 WEBKIT_FORCE_SANDBOX=0 WEBKIT_DISABLE_DMABUF_RENDERER=1 WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=wayland XDG_DATA_DIRS=/usr/local/share:/usr/share HART_REPO=/mnt/c/Users/sathi/PycharmProjects/HARTOS HART_LIQUID_PORT=6800 HART_DATA_DIR=/tmp/integ-hart-data HART_GLASS_STATUS=$GLASS_STATUS"
setsid bash -c "$GLASS_ENV; exec python3 -u /tmp/integ/integ_glass_host.py >> '$GLASS_LOG' 2>&1" &
for i in $(seq 1 140); do grep -q "WebView load FINISHED" "$GLASS_STATUS" 2>/dev/null && break; sleep 0.5; done
echo "[supp] glass: $(tail -1 $GLASS_STATUS 2>/dev/null)"
sleep 2

# Two light-ish apps: gnome-calculator (light grey) + foot
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 gnome-calculator >/tmp/supp-calc.log 2>&1 &
sleep 2.0
setsid runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME WAYLAND_DISPLAY="$HART_SOCK" WLR_RENDERER_ALLOW_SOFTWARE=1 LIBGL_ALWAYS_SOFTWARE=1 foot -T HARTOS-foot >/tmp/supp-foot.log 2>&1 &
sleep 2.0

echo ""
echo "==== M5 SNAP ZONE (correct name: left-half) ===="
H1=$(runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" raw '{"method":"window.list","args":{}}' | python3 -c "import sys,json
s=sys.stdin.read()
try:
    r=json.loads(s.split('-> ',1)[1]); w=r['result']['windows']; print(w[0]['handle'] if w else '')
except Exception: print('')" 2>/dev/null)
echo "[supp] first handle = $H1"
echo "-- place left-half --"
IPC place "$H1" left-half
echo "-- place the 2nd window right-half --"
H2=$(runuser -u sathish -- env XDG_RUNTIME_DIR=$RUNTIME python3 /tmp/integ/ipc_client.py "$IPC_SOCK" raw '{"method":"window.list","args":{}}' | python3 -c "import sys,json
s=sys.stdin.read()
try:
    r=json.loads(s.split('-> ',1)[1]); w=r['result']['windows']; print(w[1]['handle'] if len(w)>1 else '')
except Exception: print('')" 2>/dev/null)
echo "[supp] second handle = $H2"
[ -n "$H2" ] && IPC place "$H2" right-half
sleep 1.0
GD /tmp/supp-snap.png; SV 05b-M5-snap-left-half-right-half.png /tmp/supp-snap.png
echo "-- window.list after snap (geometry should be halves) --"
IPC list

echo ""
echo "==== M6 CURSOR IN FRAME ===="
# The software cursor renders at pointer.current_location(); in headless winit with
# no pointer device that defaults to (0,0) (top-left). curfind looks for the baked
# white arrow at the target, present near (0,0) + absent at a control point (1200,800).
# But (0,0) overlaps glass-shell paint; so we ALSO directly dump a 24x24 block at 0,0.
GD /tmp/supp-cursor.png; SV 09b-M6-cursor-in-frame.png /tmp/supp-cursor.png
echo "-- curfind @ (0,0) [expect present] and (1200,800) [expect absent] --"
runuser -u sathish -- python3 /tmp/integ/curfind.py /tmp/supp-cursor.png 0 0 /tmp/supp-cursor.png 1200 800 2>&1 | head -8 || echo "(curfind err)"
echo "-- direct 12x12 luminance block at (0,0) vs (1200,800) [arrow = bright cluster] --"
runuser -u sathish -- python3 - /tmp/supp-cursor.png <<'PY'
import struct, zlib, sys
def load(path):
    d=open(path,'rb').read(); i=8; w=h=ct=0; idat=b''
    while i<len(d):
        ln=struct.unpack('>I',d[i:i+4])[0]; typ=d[i+4:i+8]; chunk=d[i+8:i+8+ln]; i+=12+ln
        if typ==b'IHDR': w,h,bd,ct=struct.unpack('>IIBB',chunk[:10])
        elif typ==b'IDAT': idat+=chunk
        elif typ==b'IEND': break
    raw=zlib.decompress(idat); ch=4 if ct==6 else 3; stride=w*ch
    out=bytearray(); prev=bytes(stride); pos=0
    for y in range(h):
        f=raw[pos]; pos+=1; line=bytearray(raw[pos:pos+stride]); pos+=stride
        if f==1:
            for x in range(ch,stride): line[x]=(line[x]+line[x-ch])&255
        elif f==2:
            for x in range(stride): line[x]=(line[x]+prev[x])&255
        elif f==3:
            for x in range(stride):
                a=line[x-ch] if x>=ch else 0; line[x]=(line[x]+((a+prev[x])>>1))&255
        elif f==4:
            for x in range(stride):
                a=line[x-ch] if x>=ch else 0; b=prev[x]; c=prev[x-ch] if x>=ch else 0
                p=a+b-c; pa=abs(p-a); pb=abs(p-b); pc=abs(p-c)
                pr=a if (pa<=pb and pa<=pc) else (b if pb<=pc else c); line[x]=(line[x]+pr)&255
        out+=line; prev=bytes(line)
    return w,h,ch,bytes(out)
w,h,ch,buf=load(sys.argv[1])
def blk(x0,y0,n):
    bp=0;t=0;c=0
    for y in range(y0,min(y0+n,h)):
        for x in range(x0,min(x0+n,w)):
            o=(y*w+x)*ch; L=(buf[o]*299+buf[o+1]*587+buf[o+2]*114)//1000; t+=L; c+=1
            if L>180: bp+=1
    return t/max(c,1), bp
m,bp=blk(0,0,16); print(f'(0,0) 16x16: mean_lum={m:.0f} brightpix(>180)={bp}')
m2,bp2=blk(1200,800,16); print(f'(1200,800) 16x16: mean_lum={m2:.0f} brightpix(>180)={bp2}')
print('CURSOR-LIKE bright cluster at top-left:', 'YES' if bp>=4 and bp>bp2 else 'inconclusive')
PY

echo ""
echo "==== M6 KILLSWITCH (clean, with grim exit codes) ===="
echo "-- baseline grim (should succeed) --"
GD /tmp/supp-kbase.png
echo "-- killswitch ON --"
IPC6 screen.kill on
echo "-- grim while killed (MUST fail/refuse) --"
GD /tmp/supp-killed.png
echo "killed PNG exists? $([ -f /tmp/supp-killed.png ] && echo "YES ($(stat -c%s /tmp/supp-killed.png) bytes)" || echo NO)"
echo "-- killswitch OFF --"
IPC6 screen.kill off
sleep 0.5
echo "-- grim after restore (should succeed) --"
GD /tmp/supp-restored.png; SV 11b-M6-killswitch-on-refused-then-restored.png /tmp/supp-restored.png
echo ""
echo "-- killswitch log lines --"
strip_ansi <"$HART_LOG" | grep -iE 'screen.kill|screencopy|capture blocked|killswitch' | tail -8
echo ""
echo "-- luminance: baseline / killed / restored --"
runuser -u sathish -- python3 /tmp/integ/lum.py /tmp/supp-kbase.png /tmp/supp-killed.png /tmp/supp-restored.png 2>&1

echo ""
echo "==== FINAL composite (glass + snapped apps + cursor) ===="
GD /tmp/supp-final.png; SV 99b-FINAL-glass-plus-snapped-apps.png /tmp/supp-final.png
IPC list
echo "=== SUPP DONE ==="
