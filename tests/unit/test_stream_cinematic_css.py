"""Pytest wrapper for the STREAM cinematic-CSS behavioural guard.

The v3 "Netflix Home" pass adds hover lifts + entrance animations to the shell's
ONLY draggable element (.desktop-icon, moved each frame by an inline transform)
and to the launcher tiles. Easing or pinning that shared transform channel would
silently regress drag (lag/overshoot), replay the entrance on every tap, or kill
a tile's own hover lift. This shells out to the Node harness, which parses the
REAL stylesheet into a cascade model and asserts the resolved per-element/per-state
outcomes (CLAUDE.md Gate 5 / feedback_no_grep_tests.md). Skips if node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_stream_cinematic_css.mjs')


def test_stream_cinematic_css_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the cinematic-CSS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'cinematic-CSS harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_stream_cinematic_css_js()
    print('OK')
