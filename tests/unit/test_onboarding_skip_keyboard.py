"""Pytest driver for the onboarding NON-LOCKOUT + keyboard-navigation harness.

The real assertions live in test_onboarding_skip_keyboard.mjs beside this
file, which drives the REAL static module
(integrations/agent_engine/static/hartOnboarding.js) through a dependency-free
DOM shim and asserts OBSERVABLE behaviour (#134: a dead pointer must never
lock the user out of the full-screen overlay):

  * an actionable Skip control is rendered, focus is pulled into the modal,
    Tab / Shift+Tab cycle inside the overlay's own controls with
    preventDefault called, and Esc finishes;
  * clicking Skip closes the overlay through the same exit path Esc uses.

WHY this file exists: the .mjs was written in 2026-07 and ran in NO CI job.
flake-checks.yml discovers `tests/unit/**/test_*.py`, so a harness with no
Python driver is compiled by nobody and skipped by everybody. Same pattern as
test_hart_nav.py: skip cleanly when node is absent (CI installs node 20).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_onboarding_skip_keyboard.mjs')


def test_onboarding_skip_keyboard_js():
    """Drive the REAL hartOnboarding.js skip + focus-trap flow (Node, headless)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'onboarding skip/keyboard harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_onboarding_skip_keyboard_js()
    print('RESULT: ALL PASS')
