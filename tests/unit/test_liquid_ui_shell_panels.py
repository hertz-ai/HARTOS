"""Glass-shell panel manager — behavioural, via the REAL rendered shell JS.

LIVE-OS #20 + #21 (first real ISO/USB boot of the panel desktop):

  #20  agents/recipes/communities panels opened a BLANK body — the SPA iframe
       was injected raw, so when the backend was unreachable nothing rendered.
       The fix routes every route panel through renderRoutePanel(), which ALWAYS
       lays down a content container: a loading skeleton, the iframe (hidden),
       and — if the iframe never loads — a graceful "Reconnecting…" empty state
       with Retry. Never a blank body.

  #21  panels opened as a tiny cascade window. The fix opens them MAXIMIZED by
       default (applyMax → 100vw + .maximized class), except floating bubbles.

The .mjs harness renders the shell with the project python, slices out the
inline panel-manager <script>, and drives openPanel on a tiny DOM shim, asserting
the OBSERVABLE DOM. This wrapper shells out so pytest/CI pick it up. Skips
cleanly when node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_liquid_ui_shell_panels.mjs')


def test_shell_panels_never_blank_and_open_maximized():
    """Drive the REAL panel manager (openPanel/renderRoutePanel/applyMax) end to
    end through Node + a DOM shim and assert #20 (never-blank, skeleton→iframe→
    reconnecting) and #21 (maximized-by-default, floating-exempt)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'panel-manager harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_the_pinned_assistant_bubble_opens_nunbas_chat_page():
    """The floating 'assistant' bubble is an iframe on a Nunba route through
    the shell's proxy. It named '/social/assistant', which Nunba's router
    does not have (its chat page is '/local'; '/' redirects to hevolve.ai
    when online), so the pinned bubble opened NotFound. Found tracing N7
    gate 1 on generation 17 (2026-10-11). The harness stub above mirrors
    the real entry so the two cannot drift apart unnoticed."""
    from integrations.agent_engine.shell_manifest import PANEL_MANIFEST, PINNED_PANEL_IDS
    entry = PANEL_MANIFEST['assistant']
    assert entry['route'] == '/local', entry
    assert entry.get('floating') is True
    assert 'assistant' in PINNED_PANEL_IDS
    with open(MJS, encoding='utf-8') as fh:
        stub = fh.read()
    assert "assistant: { title:'Assistant', route:'/local'" in stub, (
        'the .mjs harness stubs the assistant entry; keep its route the real one')
