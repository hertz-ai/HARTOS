"""Pytest wrapper for the STREAM-orb redesign behavioural harness.

The orb is the OS brand centerpiece: an edgeless, breathing, brand-spectrum glow
that floats over windows. Its old guards only `node --check`'d the file or proved
it did not throw with AbortSignal removed (test_orb_webkit_safety.py) — neither
asserts the orb actually LOOKS right (no solid disc, no ring strokes, brand hues,
a real breath) or that the hero floats above app windows. This shells out to the
Node harness (test_stream_orb_brand_breathing.mjs), which drives the REAL renderer
on a recording canvas + the REAL hero on a DOM shim and asserts observable output
(CLAUDE.md Gate 5 / feedback_no_grep_tests.md). Skips cleanly if node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_stream_orb_brand_breathing.mjs')


def test_stream_orb_brand_breathing_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the orb behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the per-assertion log on failure so CI shows exactly which intent broke.
    assert r.returncode == 0, 'orb brand/breathing harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_stream_orb_brand_breathing_js()
    print('OK')
