"""Pytest driver for the shell's idle poll diet (client half).

The assertions live in test_shell_poll_diet.mjs: the REAL modules and the REAL
inline shell-state bus (sliced from the rendered shell) on the shared DOM shim,
asserting how many GETs each fallback tick issues with the SSE stream up and
down, the cadence each fallback asked for, what a push paints, that an iframed
document registers no poller, and that the clocks write the DOM only on change.
Skips cleanly where node is absent; the Python-side budget lives in
test_shell_idle_http_budget.py and runs everywhere.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_poll_diet.mjs')


def test_shell_poll_diet_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, 'poll diet harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
