"""Functional guard for the HART OS shell's floating voice orb + the WebKitGTK
JavaScript-runtime class of bug that unit tests and the React build CANNOT catch.

Why this exists: the AbortSignal.timeout() crash (NixOS 24.11 WebKitGTK lacks
that API) killed the shell's whole inline <script> at startup — toggleStartMenu
was never defined, and the orb's initHartOrb (same block) would never run. The
React build is green and Python unit tests pass, yet the shell is dead on the
ISO. The E2E OS test boots the ISO but only checks that *services* are active +
the WebKit typelib is present — it never *executes* the shell JS. This test
closes that gap cheaply (node only, no ISO build):

  * the orb renderer (voiceOrbViz.js) is actually run with AbortSignal removed,
  * the orb is wired into the shell,
  * the orb's own added JS uses no WebKitGTK-unsafe API,
  * (xfail) the whole shell routes timeouts through the _sig() fallback.

Node-only tests always run; the render tests skip if the heavy shell module
can't import (so this stays a fast CI gate).
"""
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARTOS_ROOT = os.path.dirname(HERE)
VIZ = os.path.join(HARTOS_ROOT, 'integrations', 'agent_engine', 'static', 'voiceOrbViz.js')
SHELL_SRC = os.path.join(HARTOS_ROOT, 'integrations', 'agent_engine', 'liquid_ui_service.py')
HARNESS = os.path.join(HERE, 'js', 'orb_webkit_harness.js')


def _node():
    n = shutil.which('node')
    if not n:
        pytest.skip('node not available in this environment')
    return n


def _shell_source():
    with open(SHELL_SRC, encoding='utf-8') as f:
        return f.read()


def _render_shell():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps not installed in a minimal CI runner
        pytest.skip(f'LiquidUIService not importable here: {e}')
    return LiquidUIService().render_desktop_shell()


# ── Orb renderer survives old WebKitGTK (the core lesson — behavioral) ──────

def test_voiceorbviz_syntax_ok():
    node = _node()
    r = subprocess.run([node, '--check', VIZ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_voiceorbviz_runs_without_abortsignal():
    """Run the real orb renderer with AbortSignal deleted; it must not throw."""
    node = _node()
    r = subprocess.run([node, HARNESS, VIZ], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and 'PASS' in r.stdout, (r.stdout + '\n' + r.stderr)


# ── Orb is wired into the shell + its added JS is WebKitGTK-safe ────────────

def test_orb_wired_into_shell():
    src = _shell_source()
    for needle in ('hart-voice-orb', 'voiceOrbViz.js', 'initHartOrb', 'HartVoiceOrbViz'):
        assert needle in src, f'orb wiring missing from shell source: {needle}'
    # the .mic-btn null-deref guard shipped with the orb
    assert 'if(_mb)' in src


def test_orb_init_block_has_no_webkit_unsafe_apis():
    """The orb's added inline JS must avoid APIs/syntax missing on the NixOS
    24.11 WebKitGTK (AbortSignal.timeout, template literals, optional chaining,
    nullish coalescing)."""
    src = _shell_source()
    m = re.search(r'function initHartOrb\(\).*?\}\)\(\);', src, re.S)
    assert m, 'initHartOrb block not found in shell source'
    block = m.group(0)
    assert 'AbortSignal' not in block, 'orb init uses AbortSignal (unsafe on old WebKitGTK)'
    assert '`' not in block, 'orb init uses a template literal (avoid for old WebKitGTK)'
    assert '?.' not in block, 'orb init uses optional chaining'
    assert '??' not in block, 'orb init uses nullish coalescing'


# ── Shell renders + its inline JS is valid (skips without heavy deps) ───────

def test_shell_renders_without_fstring_crash():
    html = _render_shell()
    assert len(html) > 50000, 'shell HTML suspiciously small — render may have failed'
    assert 'hart-voice-orb' in html


def test_shell_inline_js_syntax_ok(tmp_path):
    node = _node()
    html = _render_shell()
    scripts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    assert scripts, 'no inline <script> blocks found in the rendered shell'
    f = tmp_path / 'shell_inline.js'
    f.write_text('\n;\n'.join(scripts), encoding='utf-8')
    r = subprocess.run([node, '--check', str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── Shell-wide WebKitGTK fallback guard ────────────────────────────────────
# xfail until debug_1's _sig() AbortController fallback (commit 48fa0a9) is on
# this branch; it flips to xpass on merge, at which point the marker is removed
# and this becomes a hard regression guard for all 60+ fetch-timeout sites.

@pytest.mark.xfail(
    reason="main still calls raw AbortSignal.timeout() (crashes NixOS 24.11 "
           "WebKitGTK); needs debug_1's _sig() fallback (48fa0a9) merged.",
    strict=False,
)
def test_shell_routes_timeouts_through_sig_fallback():
    src = _shell_source()
    assert 'function _sig(' in src, 'the _sig() WebKitGTK fallback helper is missing'
    # only the feature-detect inside _sig may name AbortSignal.timeout
    raw = src.count('AbortSignal.timeout(')
    assert raw <= 2, (
        f'{raw} raw AbortSignal.timeout() call sites — route them through _sig() '
        'so the shell survives old WebKitGTK'
    )
