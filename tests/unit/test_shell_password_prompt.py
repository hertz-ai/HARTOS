"""Pytest driver for the first-run password prompt contract (hartSessionUI.js).

The assertions live in test_shell_password_prompt.mjs, which drives the REAL
module on the shared DOM shim and asserts the observable state of #lock-screen
and the session blob: no offer during onboarding, an offer when onboarding ends
(observed, not polled), `lock_setup_skipped` written on an explicit decline only,
never on the offer itself. Skips cleanly where node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_password_prompt.mjs')


def test_password_prompt_flag_on_decline_only_and_recheck_on_onboarding_end():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'password prompt harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
