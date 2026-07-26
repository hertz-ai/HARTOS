"""Pytest wrapper for the orb/hero drag-affordance + breathing behavioural harness.

The real coverage lives in test_orb_drag_affordances_breathing.mjs, which drives the
actual static modules (hartHero.js / hartSenses.js / voiceOrbViz.js) through their
public surface on a faithful DOM shim and asserts OBSERVABLE side-effects
(CLAUDE.md Gate 5 / feedback_no_grep_tests.md):

  FIX A — drag affordances appear ONLY while dragging:
    * the orb minimise control is revealed by the DRAG handlers (onDown/onUp),
      never by a passive hover, and stays visible while compact (restore affordance),
    * hartSenses adds/removes '.dragging' on drag start/end — the class the CSS keys
      the grip's opacity:0 -> 1 reveal to.
  FIX B — breathing rings toggle (DEFAULT ON):
    * buildOrbAura's brand rings are gated on the persisted 'hart_orb_breathing'
      flag; window.HartOrbBreathing / the orb right-click build/tear them live,
    * voiceOrbViz.setBreathing(false) dampens the canvas breathe glow.

This wrapper shells out to node so pytest/CI runs it too; it skips cleanly when
node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_orb_drag_affordances_breathing.mjs')


def test_orb_drag_affordances_breathing_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'orb drag/breathing behaviour harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_orb_drag_affordances_breathing_js()
    print('OK')
