#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Phase 5/6 integration test (the MOAT): HartWmClient arranges a REAL native
# window in a REAL sway compositor, software-rendered (pixman). LIVE-VERIFIED
# 2026-06-16 in WSL2 (sway 1.7 nested in WSLg) — an agent moved + resized the
# foot toplevel from fullscreen {0,25,1024x743} to floating {222,203,500x325}.
#
# Runs in any Wayland session (WSLg, a nixosTest VM, real hardware). Exits
# non-zero on failure. Wayland/sway/foot required — this is the OS-native
# windowing path, NOT a dev-box pytest. See compositor/IPC_PROTOCOL.md +
# ROADMAP.md Phase 5/6.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
REPO="${HART_REPO:-/mnt/c/Users/sathi/PycharmProjects/HARTOS}"

command -v sway >/dev/null || { echo "FAIL: sway not installed"; exit 2; }
command -v foot >/dev/null || { echo "FAIL: foot not installed"; exit 2; }
[ -z "${XDG_RUNTIME_DIR:-}" ] && export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
[ -z "${WAYLAND_DISPLAY:-}" ] && export WAYLAND_DISPLAY=wayland-0
# Nest in the parent Wayland session; mandatory software render (the never-fail
# floor renderer) — proves the moat with no GPU.
export WLR_BACKENDS=wayland WLR_RENDERER=pixman

cat > /tmp/hart_wm_integ.py <<'PYEOF'
from integrations.agent_engine.hart_wm_client import HartWmClient
c = HartWmClient()
c._backend = "sway"
wins = c.list_windows()
foot = next((w for w in wins if w["app_id"] == "foot"), None)
assert foot, f"sway should host the foot toplevel; got {wins}"
assert c.place_window(foot["id"], 120, 90, 500, 350)["ok"], "place_window failed"
f2 = next((w for w in c.list_windows() if w["id"] == foot["id"]), None)
assert f2, "window vanished after place"
r = f2["rect"]
assert r["x"] > 0 and r["width"] <= 700, f"window did not move/resize: {r}"
assert c.focus_window(foot["id"])["ok"], "focus_window failed"
print("PASS: HartWmClient arranged a real window in real sway:", r)
PYEOF

cat > /tmp/hart_sway.cfg <<CFG
output WL-1 resolution 1024x768 background #0b1020 solid_color
exec foot
exec sh -c "sleep 5; cd '$REPO'; PYTHONPATH=. python3 /tmp/hart_wm_integ.py > /tmp/hart_wm_integ.out 2>&1; swaymsg exit"
CFG

timeout 60 sway -c /tmp/hart_sway.cfg >/tmp/hart_sway.log 2>&1 || true
echo "--- HartWmClient vs real sway ---"
cat /tmp/hart_wm_integ.out 2>/dev/null || { echo "FAIL: no integration output"; exit 1; }
grep -q "^PASS:" /tmp/hart_wm_integ.out || { echo "FAIL: moat assertion failed"; exit 1; }
echo "OK: Phase 5/6 moat verified — agent arranged a real window in a real compositor"
