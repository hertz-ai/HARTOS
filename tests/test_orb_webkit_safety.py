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
# debug_1's _sig() AbortController fallback is now merged (PR #73), so every
# inline fetch-timeout site routes through it. This is now a HARD regression
# guard: only the _sig() feature-detect itself may name AbortSignal.timeout().
def test_shell_routes_timeouts_through_sig_fallback():
    src = _shell_source()
    assert 'function _sig(' in src, 'the _sig() WebKitGTK fallback helper is missing'
    # only the feature-detect inside _sig may name AbortSignal.timeout
    raw = src.count('AbortSignal.timeout(')
    assert raw <= 2, (
        f'{raw} raw AbortSignal.timeout() call sites — route them through _sig() '
        'so the shell survives old WebKitGTK'
    )


# ── ALL static shell JS is present, valid, and WebKitGTK-safe ───────────────
# The shell grew from one orb script to a fleet of static modules (hartSession,
# hartHero, hartDesktop, hartWorkspaces, hartPersonalize, hartMarketplace,
# hartDock). Each is a separate <script src>, so a syntax error or an
# AbortSignal.timeout() in ANY of them silently kills that feature on the ISO.
# These guards extend the orb's lesson to every static module (node + source
# only, so they run in the minimal shell-ui CI).

STATIC_DIR = os.path.join(HARTOS_ROOT, 'integrations', 'agent_engine', 'static')


def _static_js():
    return sorted(f for f in os.listdir(STATIC_DIR) if f.endswith('.js'))


def test_all_static_js_syntax_ok():
    node = _node()
    for name in _static_js():
        r = subprocess.run([node, '--check', os.path.join(STATIC_DIR, name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, name + ': ' + r.stderr


def test_static_js_is_webkitgtk_safe():
    """AbortSignal.timeout() (the NixOS 24.11 crasher) may appear ONCE — inside
    the HartTimeoutSignal feature-detect wrapper in hartSession.js — and nowhere
    else; no template literals / optional chaining / nullish coalescing either."""
    raw_total = 0
    for name in _static_js():
        # Vendored, minified third-party bundles (e.g. lottie.min.js — the offline
        # Hevolve boot-splash player) are NOT HART-authored: we cannot rewrite their ES
        # syntax, and their browser-compat is the vendor's concern (they are still
        # parse-checked by test_all_static_js_syntax_ok via `node --check`). These bans
        # are a HART CODE-STYLE rule for the shell JS we OWN, so skip vendored *.min.js.
        if name.endswith('.min.js'):
            continue
        src = open(os.path.join(STATIC_DIR, name), encoding='utf-8').read()
        n = src.count('AbortSignal.timeout(')
        raw_total += n
        if name != 'hartSession.js':
            assert n == 0, name + ' calls AbortSignal.timeout() directly (use window.HartTimeoutSignal)'
        # Plain substring check ON PURPOSE, so it also trips on backticks inside
        # comments. Parsing JS well enough to skip comments is how a safety test
        # acquires its own bugs (a naive `//` strip eats the rest of any line
        # holding an https:// URL), and "no backticks at all in shipped shell
        # JS" is a rule a reader can apply without running anything.
        assert '`' not in src, (
            name + ' contains a backtick. Old WebKitGTK cannot parse template '
            'literals, and this guard greps raw source, so comments count too: '
            "quote with 'single quotes' instead."
        )
        assert '?.' not in src, name + ' uses optional chaining'
        assert '??' not in src, name + ' uses nullish coalescing'
    assert raw_total <= 1, 'AbortSignal.timeout() should exist only in the HartTimeoutSignal wrapper'


def test_referenced_static_assets_exist():
    """Every /shell/static/<asset> the shell references must exist on disk — a
    missing file means the shell serves a 404 and that feature silently dies
    (the bundling end-to-end gap)."""
    src = _shell_source()
    # Capture the FULL path after /shell/static/ (incl. nested dirs like app_art/x.svg);
    # `/` is in the class so nested asset references resolve to the real file, not just
    # the first path segment. A trailing-slash reference (a startswith dir prefix, e.g.
    # '/shell/static/app_art/') resolves to the directory.
    refs = set(re.findall(r'/shell/static/([A-Za-z0-9_.\-/]+)', src))
    assert refs, 'no /shell/static refs found — wiring regression'
    for asset in sorted(refs):
        assert os.path.exists(os.path.join(STATIC_DIR, asset.rstrip('/'))), \
            'referenced but missing on disk: ' + asset
