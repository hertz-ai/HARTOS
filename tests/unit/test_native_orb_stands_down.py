"""The browser must stop drawing what the compositor already draws.

THE DEFECT, measured on the Samsung box 2026-09-07 with the native-chrome
bridge fully engaged (/run/hart/session/native-chrome = "bloom,orb", and the
shell logging "NATIVE CHROME: surface transparent"):

    WebKitWebProcess   1188 CPU ticks in 12s   -- a full core
    hart-comp          layer.composited fired ONCE this entire boot
    hart-latency       zero samples from 943 pointer + 104 key events

A full core burned, and not one page flip to show for it. The handoff had
stopped the canvas PAINTING and left the script DRAWING: voiceOrbViz runs a
self-perpetuating requestAnimationFrame loop, and rAF throttling keys off
document visibility, not element visibility, so `visibility:hidden` on the
canvas changed nothing about the cost. liquid_ui_service's own comment
predicted exactly this ("the browser would still be paying the per-frame cost
M2 exists to remove"); the CSS-only half never removed it.

The behavioural JS coverage lives in the .mjs beside this file, which drives
the REAL shipped module and COUNTS frames. A source grep would have passed
before the fix, because the CSS rule it would have found was already there.
"""
import os
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest

import integrations.agent_engine.liquid_ui_service as L

MJS = os.path.join(os.path.dirname(__file__), 'test_native_orb_stands_down.mjs')


def test_orb_loop_stands_down_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'orb stand-down harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def _svc():
    s = L.LiquidUIService.__new__(L.LiquidUIService)
    s._data_dir = os.environ.get('TEMP', '.')
    s.port = 6800
    s.renderer = 'webkit'
    s.theme = 'auto'
    s.voice_enabled = True
    s.haptic_enabled = False
    s.context_refresh_ms = 2000
    s.a2ui_enabled = True
    s.model_bus_port = 6790
    s.backend_port = 6777
    s._model_available = False
    return s


def _render(claim):
    with patch.object(L, 'read_native_chrome', return_value=frozenset(claim)):
        return _svc().render_desktop_shell()


def _claim_js(html):
    m = re.search(r'window\.HART_NATIVE_CHROME = (.+?);', html)
    return m.group(1) if m else None


def test_the_claim_reaches_script_not_only_css():
    """CSS cannot stop a script, so the verdict has to reach JS as well."""
    html = _render({'bloom', 'orb'})
    assert _claim_js(html) == '["bloom", "orb"]', _claim_js(html)


def test_css_and_script_act_on_the_SAME_verdict():
    """One publisher, several consumers. If these two ever disagree, the orb is
    either drawn twice or not at all."""
    owned = _render({'bloom', 'orb'})
    assert 'hart-hero-orbwrap>canvas' in owned      # CSS hides it
    assert '"orb"' in _claim_js(owned)              # and script stands down

    free = _render(set())
    assert 'hart-hero-orbwrap>canvas' not in free   # CSS leaves it visible
    assert _claim_js(free) == '[]'                  # and script keeps drawing


def test_no_claim_emits_an_empty_array_not_a_missing_global():
    """The orb module reads the global defensively, but an undefined global on
    a page that HAS a compositor would be an ambiguity worth avoiding."""
    assert _claim_js(_render(set())) == '[]'
