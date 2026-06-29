"""Phase 8 effects must ALL be gated behind the software-render fallback.

The never-fail floor (compositor/ROADMAP Phase 8 + hart-sway-tier1.nix): every
desktop effect — the animated colour ambient, the glass blur, the panel
open/close/minimize transitions, and the hartEffects.js snap-zones — MUST be
gated so a broken / software-GL GPU degrades to a FLAT desktop, never a black or
janky one. The single gate is the perf "potato" tier (ThemeService' performance
profile, ``disable_blur``), mirrored to the browser as ``window.HART_PERF.potato``
so the external /shell/static effects module reads the SAME gate.

Behavioural: render the REAL shell HTML in both tiers and assert the observable
HTML differs exactly at the effects, plus serve the effects script. Not a
source-shape test — it drives ``render_desktop_shell`` and the live static route.

    python -m pytest tests/unit/test_phase8_effects_gating.py -q
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _render(potato: bool) -> str:
    """Render the shell with the perf tier forced on/off via ThemeService.

    The GPU verdict is pinned to 'hardware' (a CAPABLE GPU) so this file isolates
    the THEME-tier gate (``disable_blur``). The orthogonal software-render gate —
    potato also turns on when ``read_gpu_render_mode()`` is 'software' — is
    covered behaviourally in test_shell_software_render_perf.py.
    """
    theme = {'performance': {'disable_blur': potato}}
    with patch('integrations.agent_engine.theme_service.ThemeService'
               '.get_active_theme', return_value=theme), \
         patch('integrations.agent_engine.theme_service.ThemeService'
               '.get_css_variables', return_value=':root{--hart-accent:#00D4AA}'), \
         patch('integrations.agent_engine.liquid_ui_service'
               '.read_gpu_render_mode', return_value='hardware'):
        return LiquidUIService().render_desktop_shell()


@pytest.fixture(scope='module')
def html_full():
    return _render(potato=False)


@pytest.fixture(scope='module')
def html_potato():
    return _render(potato=True)


# ── The animated ambient (de-monochrome living colour) ──

def test_animated_ambient_present_on_capable_gpu(html_full):
    # The drifting multi-hue ambient layer + its keyframe animation are emitted.
    assert 'class="hart-ambient"' in html_full
    assert '@keyframes hart-ambient-drift' in html_full


def test_animated_ambient_suppressed_on_potato(html_potato):
    """Broken/software GL: the ambient DIV is not even emitted → flat desktop,
    no per-frame blur-rasterised animated layer. Never black: the wallpaper +
    static vignette still paint."""
    assert 'class="hart-ambient"' not in html_potato
    assert 'class="hart-grain"' not in html_potato
    # The non-animated vignette still renders, so the desktop is never blank.
    assert 'class="hart-vignette"' in html_potato


# ── Panel open/close transitions ──

def test_panel_transitions_present_on_capable_gpu(html_full):
    assert '.panel.closing' in html_full
    assert '.panel.minimizing' in html_full
    assert 'animation:fadeIn' in html_full


def test_panel_transitions_disabled_on_potato(html_potato):
    # The animation CSS block is swapped for the no-animations stub.
    assert 'animations disabled for performance' in html_potato
    assert '.panel.closing' not in html_potato


# ── The single gate the JS effects module reads ──

def test_perf_potato_gate_mirrored_to_window_for_js_effects(html_full, html_potato):
    """hartEffects.js self-gates on window.HART_PERF.potato — the SAME flag the
    inline PERF const carries. Both tiers must expose it so a runtime-loaded
    effect installs nothing on a potato box."""
    assert 'window.HART_PERF = PERF' in html_full
    assert 'potato: false' in html_full
    assert 'potato: true' in html_potato


def test_effects_and_workspaces_scripts_are_referenced(html_full):
    # The snap-zones effects module + the workspace switcher are wired into the
    # served shell (they self-gate at runtime; presence here proves the wire).
    assert '/shell/static/hartEffects.js' in html_full
    assert '/shell/static/hartWorkspaces.js' in html_full


def test_canonical_snap_exposed_for_effects_module(html_full):
    """hartEffects.js commits a snap via the canonical window.snapPanel — no
    parallel snap geometry. The shell must expose it."""
    assert 'window.snapPanel = snapPanel' in html_full


# ── The effects script actually serves (would be a dead snap-zone otherwise) ──

def test_hart_effects_js_is_served():
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    r = app.test_client().get('/shell/static/hartEffects.js')
    assert r.status_code == 200 and r.data
    # The drag-lifecycle hooks it listens on (canonical, no forked mousedown).
    body = r.get_data(as_text=True)
    assert 'hart:dragstart' in body and 'hart:dragend' in body
