"""Shell snap-zones EFFECT (Phase 8) — behavioural, via the real hartEffects.js.

Runs integrations/agent_engine/static/hartEffects.js through the shell's
canonical drag lifecycle on a tiny DOM shim (test_shell_effects_snapzones.mjs)
and asserts:
  - a near-edge drag arms the single reused snap-zone and releasing commits via
    the CANONICAL window.snapPanel (one snap geometry, no parallel path);
  - the never-fail gate: on the software-GL floor (window.HART_PERF.potato),
    prefers-reduced-motion, or the live html.a11y-rmotion class, the same gesture
    installs NOTHING and snaps zero times — the desktop degrades FLAT, never
    janky/black.

Skips cleanly when node is absent (the .mjs runs standalone in CI; node 20 is
present there). Paired with the Python render-gating test
(test_phase8_effects_gating.py) which proves the CSS-side effects (animated
ambient + panel transitions) are gated by the same potato tier.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_effects_snapzones.mjs')


def test_snapzones_effect_behaviour():
    """Drive the REAL hartEffects.js snap-zone flow end-to-end (Node + DOM shim)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the per-assertion log on failure so CI shows exactly which broke.
    assert r.returncode == 0, 'hartEffects.js snap-zone harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
