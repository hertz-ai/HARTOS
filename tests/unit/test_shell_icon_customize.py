"""Desktop-icon CUSTOMIZATION (glyph / label / color), macOS-/Windows-style.

Two behavioural layers, no grep-tests:

1. test_icon_customize_behaviour — runs the REAL static/hartDesktop.js through
   its public API on a tiny DOM shim (test_shell_icon_customize.mjs) and asserts
   the full round-trip: open the Customize dialog -> edit glyph/label/color ->
   Save -> the icon ELEMENT mutates AND HartSession persists the override; plus
   emoji-vs-Material glyph rendering and Reset. Skips cleanly if node is absent.

2. test_session_state_roundtrips_icon_overrides — proves the persistence contract
   the feature relies on through the REAL Flask route: a desktop_icons entry that
   carries glyph/label/color survives POST -> GET unchanged (opaque blob), and
   un-customized siblings + unrelated shell state are not clobbered.

Local note: this box OOM-kills the full pytest import chain; the .mjs runs
standalone (`node tests/unit/test_shell_icon_customize.mjs`) and the round-trip
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

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_icon_customize.mjs')


def _liquid_ui():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps absent in a minimal CI runner -> skip
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def test_icon_customize_behaviour():
    """Drive the REAL hartDesktop.js customize flow end-to-end (Node + DOM shim)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the harness' per-assertion log on failure so CI shows exactly which.
    assert r.returncode == 0, 'hartDesktop.js customize harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_session_state_roundtrips_icon_overrides():
    """An icon entry carrying glyph/label/color survives the REAL POST->GET route
    unchanged, and neither un-customized siblings nor other shell state are lost
    (the client read-modify-writes one blob; the backend stores it opaquely)."""
    svc = _liquid_ui()()
    svc._data_dir = tempfile.mkdtemp()  # never touch real shell state
    client = svc._create_flask_app().test_client()

    icons = [
        {'id': 'feed', 'x': 24, 'y': 24,
         'glyph': 'auto_awesome', 'label': 'My Feed', 'color': '#ff8800'},  # customized
        {'id': 'recipes', 'x': 24, 'y': 116},                               # plain
        {'id': 'notes', 'x': 24, 'y': 208, 'glyph': '\U0001f680'},          # emoji glyph only
    ]
    r = client.post('/api/shell/session-state',
                    json={'desktop_icons': icons, 'theme': 'midnight'})
    assert r.status_code == 200

    got = client.get('/api/shell/session-state').get_json()
    assert got.get('desktop_icons') == icons          # overrides + emoji round-trip intact
    assert got.get('theme') == 'midnight'             # unrelated shell state not clobbered


def test_hartdesktop_js_wires_the_customize_action():
    """Source-shape guard (explicitly labelled — paired with the behavioural test
    above, never the only coverage): the context menu offers Customize and the
    handler/applier the menu invokes are defined by name, and customization
    travels in the single desktop_icons writer (no parallel override store)."""
    src = open(os.path.join(ROOT, 'integrations', 'agent_engine', 'static', 'hartDesktop.js'),
               encoding='utf-8').read()
    # WAS: assert "'Customize" in src and "hartCustomizeIcon('" in src
    # Red since at least 2026-07-31 (six consecutive runs of the Shell UI
    # WebKit Safety gate) while the FEATURE WORKED — and wrong in a more
    # instructive way than a plain rename:
    #   * "hartCustomizeIcon('" appears ZERO times. The call sites are
    #     `window.hartCustomizeIcon && window.hartCustomizeIcon(id)` (:345)
    #     and the definition (:389). The literal open-quote could never have
    #     matched this code, so this half was simply wrong.
    #   * "'Customize" DOES still match — but at :401, as
    #     `ov.setAttribute('aria-label', 'Customize icon')` inside the dialog.
    #     The MENU entry it claims to check was renamed to 'Properties'
    #     (:345). So this half was passing for a coincidental reason, in a
    #     different part of the file, about a different thing.
    # A guard half-passing by accident and half-failing on a string that never
    # existed is exactly what feedback_no_grep_tests warns about: it says
    # nothing about whether the wiring survived, in either direction.
    #
    # Assert the WIRING instead: some context-menu entry must invoke the
    # action, and the action must be defined. That is what "menu entry ->
    # action" was always trying to say, and it survives renaming the label.
    assert 'window.hartCustomizeIcon =' in src                    # action defined
    _menu_entry = [ln for ln in src.splitlines()
                   if 'label:' in ln and 'hartCustomizeIcon' in ln]
    assert _menu_entry, (
        "no context-menu entry invokes hartCustomizeIcon — the action is "
        "defined but unreachable from the menu, which is the regression this "
        "guard exists to catch (the LABEL may be renamed freely)")
    assert 'function applyIconVisual' in src                      # single apply path (render + dialog)
    # one writer: overrides ride inside readPositions()/persist(), not a new key
    assert "HartSession.set('desktop_icons'" in src
    assert 'data-ov-glyph' in src                                 # serialized off the DOM, no side store


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
