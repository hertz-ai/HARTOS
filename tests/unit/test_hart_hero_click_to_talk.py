"""Pytest wrapper for the hartHero click-to-talk behavioural harness (#123 / W9).

The real coverage lives in test_hart_hero_click_to_talk.mjs, which drives the
actual static module (hartHero.js) through its public surface on a faithful DOM
shim and asserts OBSERVABLE side-effects (CLAUDE.md Gate 5 / feedback_no_grep_tests):

  * the orb click / Enter / Space all funnel into the shell's canonical STT entry
    (window.toggleVoice — MediaRecorder -> /api/voice -> model_bus _route_stt),
  * window.HartHeroTalk exposes that SAME entry (no parallel mic path) and degrades
    to false (never throws) when the shell voice API is absent,
  * a recognized transcript is reflected into the hero bar for the agent turn.

This wrapper shells out to node so pytest/CI runs it too; it skips when node is
absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_hart_hero_click_to_talk.mjs')


def test_hart_hero_click_to_talk_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'hartHero click-to-talk harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_hart_hero_click_to_talk_js()
    print('OK')
