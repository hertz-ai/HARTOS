"""Behavioural coverage for the HART OS shell OVERHAUL interaction logic.

The overhaul's five behaviours (contextual/deterministic visibility, active-state
lighting, the floating sensory pod, the orb-click voice toggle, virtual desktops)
were previously guarded only by HTML-substring presence + the icon-customize
dialog .mjs — none drove the real state machines. That gap let the data-speaking
dead-write and the _hartThinking timer race ship green (CLAUDE.md Gate 5 /
feedback_no_grep_tests.md). This adds the missing behavioural layer:

1. test_overhaul_behaviours_js — runs the REAL static modules (hartVisibility /
   hartHero / hartSenses / hartWorkspaces) through their public surface on a tiny
   DOM shim (test_shell_overhaul_behaviors.mjs) and asserts OBSERVABLE side-effects:
     * the state->attribute engine stamps every <html data-*> incl. the 3 signals
       the review flagged (data-speaking / data-agents / data-online),
     * hartHero.dispatch() does NOT arm a fixed-duration _hartThinking clear (the
       race) — acSend stays the sole writer; the orb/dot mirror the real flag,
     * hartSenses.restore() RE-CLAMPS a stale saved pod position to the current
       viewport, and the eye lamp lights for ANY live sense (not camera-only),
     * the workspace pager reveals (data-multiws=1) once the feature is USABLE,
       breaking the old ">1 occupied" discoverability deadlock.
   Skips cleanly if node is absent.

2. test_senses_pos_roundtrips — proves the persistence contract the floating pod
   relies on through the REAL Flask /api/shell/session-state route: a senses_pos
   {x,y,edge} blob survives POST -> GET unchanged and does not clobber siblings.

Local note: this box OOM-kills the full pytest import chain; the .mjs runs
standalone (`node tests/unit/test_shell_overhaul_behaviors.mjs`) and the round-trip
runs through a temp-dir Flask client. Committed for CI (node 20 present there).
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_overhaul_behaviors.mjs')


def _liquid_ui():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps absent in a minimal CI runner -> skip
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def test_overhaul_behaviours_js():
    """Drive the REAL overhaul modules end-to-end (Node + DOM shim)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the harness' per-assertion log on failure so CI shows exactly which.
    assert r.returncode == 0, 'shell-overhaul behaviour harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_senses_pos_roundtrips():
    """The floating pod's persisted {x,y,edge} survives the REAL POST->GET route
    unchanged, and an unrelated shell-state key is not clobbered (the client
    read-modify-writes one blob; the backend stores it opaquely)."""
    svc = _liquid_ui()()
    svc._data_dir = tempfile.mkdtemp()  # never touch real shell state
    client = svc._create_flask_app().test_client()

    pos = {'x': 1208, 'y': 678, 'edge': 'rb'}
    r = client.post('/api/shell/session-state',
                    json={'senses_pos': pos, 'theme': 'midnight'})
    assert r.status_code == 200

    got = client.get('/api/shell/session-state').get_json()
    assert got.get('senses_pos') == pos        # the pod position round-trips intact
    assert got.get('theme') == 'midnight'       # unrelated shell state not clobbered


def test_shell_html_seeds_data_multiws_and_no_hero_mic():
    """Source/served-shape guards (explicitly labelled — paired with the
    behavioural .mjs above, never the only coverage):

      * the <html> markup SEEDS data-multiws="0" so the pager's hide rule matches
        at first paint (an ABSENT attr would not -> reveal FOUC), and
      * there is NO #hart-hero-mic — the orb itself is the click-to-talk control
        (the central mic glyph was removed in the overhaul).
    """
    svc = _liquid_ui()()
    svc._data_dir = tempfile.mkdtemp()
    html = svc._create_flask_app().test_client().get('/').get_data(as_text=True)
    assert 'data-multiws="0"' in html          # FOUC-killing seed present at first paint
    assert 'id="hart-hero-mic"' not in html     # no central mic glyph; the orb is the control
    assert 'id="hart-voice-orb"' in html        # the orb the click-to-talk wiring targets


if __name__ == '__main__':
    # Inline runner (pytest OOMs on this box): execute every test_* and report.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1
            print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
