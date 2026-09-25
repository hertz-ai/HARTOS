"""Pytest driver for the onboarding SPEAKS-its-ceremony harness (checklist V1).

The real assertions live in test_onboarding_speak.mjs beside this file, which
drives the REAL static module (integrations/agent_engine/static/hartOnboarding.js)
through a dependency-free DOM shim and asserts OBSERVABLE behaviour:

  * when the "Light Your HART" overlay opens, window.speakText is CALLED with
    the narration text and source 'onboarding' (the ONE shell TTS path,
    HOME_DESKTOP_DESIGN_CHECKLIST h2 + b8);
  * with #hart-hero.ai-blind set (the kill-switch flag hartSenses.js writes)
    speakText is NOT called while the overlay still opens and still renders
    the line: the ceremony shows, it just does not talk.

WHY this file exists: HOME_DESKTOP_DESIGN_CHECKLIST.md row V1 cites this .mjs
as its evidence, and until now no CI job executed it (flake-checks.yml only
discovers `test_*.py`). A cited test that never runs is not evidence. Same
pattern as test_hart_nav.py: skip cleanly when node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_onboarding_speak.mjs')


def test_onboarding_speak_js():
    """Drive the REAL hartOnboarding.js speak / ai-blind flow (Node, headless)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'onboarding speak harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_onboarding_speak_js()
    print('RESULT: ALL PASS')
