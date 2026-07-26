#!/usr/bin/env bash
# Launch the headless sway host (as sathish) detached, then wait for its socket.
# Run THIS as root; it drops to sathish via runuser. Keeps the nesting shallow so
# wsl.exe -> bash -lc quoting never mangles redirections.
set -u
pkill -u sathish sway 2>/dev/null || true
pkill -f "target/debug/hart-comp" 2>/dev/null || true
pkill swaybg 2>/dev/null || true
sleep 1
rm -f /run/user/1000/wayland-host /run/user/1000/wayland-host.lock 2>/dev/null || true

setsid runuser -u sathish -- /tmp/m2_host_sway.sh >/tmp/m2-sway-host.stdout 2>&1 &
echo "launched headless sway host (pid-group $!)"

ok=no
for i in $(seq 1 30); do
  if [ -S /run/user/1000/wayland-host ]; then ok=yes; echo "HOST SOCKET UP after $((i))x0.5s"; break; fi
  sleep 0.5
done
if [ "$ok" = no ]; then echo "HOST SOCKET DID NOT APPEAR"; fi
ls -la /run/user/1000/wayland-host* 2>/dev/null || true
echo "=== sway host log (tail) ==="
tail -25 /tmp/m2-sway-host.log 2>/dev/null || true
