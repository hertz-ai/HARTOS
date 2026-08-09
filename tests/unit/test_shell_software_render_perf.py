"""#137 reduced-effects-on-software-render + #138 terminal-exec re-render guard,
through the REAL LiquidUIService render + verdict reader.

#137: hart-gpu-probe writes `hardware`/`software` to /run/hart/gpu-render. The
desktop shell reads that SAME verdict (no second probe) and tags <body> with
`gpu-software` / `gpu-hardware`; hartResponsive.css keys the reduced-effects
floor off `body.gpu-software`. Behavioural (real reader + real render, the file
boundary mocked) per the no-grep-tests rule.

#138: the terminal exec must not be cancelled / recreated by a panel re-render —
guarded in inline shell JS (no Python entry point), so a clearly-labelled
source guard backs the behavioural #137 coverage.
"""
import os
import re

import pytest

from integrations.agent_engine import liquid_ui_service as lus
from integrations.agent_engine.liquid_ui_service import (
    LiquidUIService, read_gpu_render_mode)


# ── #137 verdict reader — fail-SOFTWARE contract ──────────────────────────────

def test_verdict_reader_returns_hardware_only_on_exact_match(tmp_path, monkeypatch):
    f = tmp_path / 'gpu-render'
    f.write_text('hardware\n')
    monkeypatch.setattr(lus, '_GPU_RENDER_VERDICT_FILE', str(f))
    assert read_gpu_render_mode() == 'hardware'


def test_verdict_reader_software_value(tmp_path, monkeypatch):
    f = tmp_path / 'gpu-render'
    f.write_text('software\n')
    monkeypatch.setattr(lus, '_GPU_RENDER_VERDICT_FILE', str(f))
    assert read_gpu_render_mode() == 'software'


def test_verdict_reader_missing_file_fails_software(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lus, '_GPU_RENDER_VERDICT_FILE', str(tmp_path / 'does-not-exist'))
    assert read_gpu_render_mode() == 'software'


def test_verdict_reader_unexpected_value_fails_software(tmp_path, monkeypatch):
    f = tmp_path / 'gpu-render'
    f.write_text('llvmpipe-maybe?')
    monkeypatch.setattr(lus, '_GPU_RENDER_VERDICT_FILE', str(f))
    assert read_gpu_render_mode() == 'software'


# ── #137 render tags <body> from the verdict ──────────────────────────────────

@pytest.fixture
def svc():
    return LiquidUIService()


def _body_tag(html):
    m = re.search(r'<body[^>]*>', html)
    assert m, 'no <body> tag in rendered shell'
    return m.group(0)


def test_render_tags_body_gpu_software(svc, monkeypatch):
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'software')
    body = _body_tag(svc.render_desktop_shell())
    assert 'gpu-software' in body
    assert 'gpu-hardware' not in body


def test_render_tags_body_gpu_hardware(svc, monkeypatch):
    # gpu-hardware now requires BOTH a hardware probe AND WebKit compositing on
    # (preferHardwareGL), because the shell's per-frame effects run in the WebView
    # (cairo unless compositing is on). A hardware probe with compositing OFF is a
    # cairo box → gpu-software (see test_render_hardware_probe_but_cairo_is_software).
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    body = _body_tag(svc.render_desktop_shell())
    assert 'gpu-hardware' in body
    assert 'gpu-software' not in body


def test_render_hardware_probe_but_cairo_is_software(svc, monkeypatch):
    """The real-HW 2026-07-12 fix: a box whose gpu-probe says 'hardware' but whose
    WebView paints on cairo (preferHardwareGL unset → compositing OFF) must tag
    <body gpu-software>, NOT gpu-hardware. Arming the GPU-only cinematic (hover
    transforms, animated filter:blur) on a CPU renderer pegged the single WebKit
    thread and HUNG the whole shell on orb hover, and left the static-colour floor
    disengaged so the desktop read flat/monochromatic. The effect tier must track
    the ACTUAL paint path, not just the compositor probe."""
    monkeypatch.delenv('LIQUID_UI_PREFER_HW_GL', raising=False)
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    body = _body_tag(svc.render_desktop_shell())
    assert 'gpu-software' in body, (
        'a hardware-probe box with WebKit compositing OFF is a cairo renderer → the '
        'shell effect tier must be gpu-software (else the orb-hover hang + flat floor)')
    assert 'gpu-hardware' not in body


def _perf_potato_line(html):
    """The rendered inline-script PERF.potato flag (the JS reduced-effects gate
    hartEffects.js / hartDock.js read). Returns 'true' or 'false'."""
    m = re.search(r'potato:\s*(true|false)', html)
    assert m, 'no PERF.potato flag in rendered shell'
    return m.group(1)


def test_perf_potato_true_under_software_verdict(svc, monkeypatch):
    """CAUSE 3: on a software-rendered box the JS reduced-effects gate must
    engage — PERF.potato true — even when the THEME tier leaves blur ON. Wiring
    the potato gate to the SAME verdict the CSS floor uses is what kills the
    ~500ms keystroke lag without a GPU."""
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'software')
    # Theme tier explicitly NOT potato, so the only trigger is the GPU verdict.
    monkeypatch.setattr(
        'integrations.agent_engine.theme_service.ThemeService.get_active_theme',
        lambda: {'performance': {'disable_blur': False}})
    html = svc.render_desktop_shell()
    assert _perf_potato_line(html) == 'true'
    assert 'gpu-software' in _body_tag(html)


def test_perf_potato_false_under_hardware_verdict_and_capable_theme(svc, monkeypatch):
    """Converse: a capable GPU + WebKit compositing on + a non-potato theme keeps
    the full cinematic (PERF.potato false). Compositing must be opted in
    (preferHardwareGL) for the hardware effect tier — a hardware probe alone on a
    cairo WebView is gpu-software (the real-HW hover-hang fix)."""
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    monkeypatch.setattr(
        'integrations.agent_engine.theme_service.ThemeService.get_active_theme',
        lambda: {'performance': {'disable_blur': False}})
    html = svc.render_desktop_shell()
    assert _perf_potato_line(html) == 'false'
    assert 'gpu-hardware' in _body_tag(html)


def test_perf_potato_true_when_theme_disables_blur_on_capable_gpu(svc, monkeypatch):
    """The theme tier still forces potato independently of the GPU: disable_blur
    True on a HARDWARE box stays potato (the two gates OR together)."""
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    monkeypatch.setattr(
        'integrations.agent_engine.theme_service.ThemeService.get_active_theme',
        lambda: {'performance': {'disable_blur': True}})
    html = svc.render_desktop_shell()
    assert _perf_potato_line(html) == 'true'


def test_software_render_css_floor_present(svc, monkeypatch):
    """The reduced-effects floor must exist in the served stylesheet, keyed off
    body.gpu-software, and kill the per-frame GPU cost (backdrop-filter)."""
    app = svc._create_flask_app()
    app.testing = True
    client = app.test_client()
    r = client.get('/shell/static/hartResponsive.css')
    assert r.status_code == 200
    css = r.get_data(as_text=True)
    assert 'body.gpu-software' in css
    # The keystroke hot-path kill: blur is disabled on the frosted surfaces.
    assert re.search(r'body\.gpu-software[^{]*\{[^}]*backdrop-filter:\s*none', css)


# ── #151 transparent-windows: solidify the glass when WebKit compositing is OFF ──
# The frosted .glass/.panel lean on backdrop-filter:blur, which paints ONLY with
# WebKit accelerated compositing (preferHardwareGL=true). With it off (the default)
# the blur paints nothing and a translucent panel reads SEE-THROUGH. The fix tags
# <body webkit-flat> from LIQUID_UI_PREFER_HW_GL — decoupled from the gpu verdict —
# and the CSS floor solidifies the glass on that class. Behavioural (real render +
# real served stylesheet), the env boundary set explicitly.

def test_body_webkit_flat_when_compositing_off_even_on_hardware_probe(svc, monkeypatch):
    """The #151 regression case: a box whose gpu-probe says `hardware` but whose
    WebKit compositing is OFF (preferHardwareGL unset/false) must STILL tag
    webkit-flat so the panels are opaque, not see-through. This is the decoupling
    the old probe-only body class missed."""
    monkeypatch.delenv('LIQUID_UI_PREFER_HW_GL', raising=False)
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    body = _body_tag(svc.render_desktop_shell())
    assert 'webkit-flat' in body, (
        "compositing-off render must tag <body webkit-flat> to solidify the glass; "
        "without it translucent panels render see-through (#151)")
    # The effect tier now also reflects the cairo reality (2026-07-12): a hardware
    # probe with compositing OFF is gpu-software, so the GPU-only per-frame effects
    # shed (fixes the orb-hover hang) AND the glass solidifies via BOTH triggers.
    assert 'gpu-software' in body
    assert 'gpu-hardware' not in body


def test_webkit_flat_keyed_off_the_vulkan_rung_not_the_gl_flag(svc, monkeypatch):
    """The glass frost is keyed off the RUNTIME RENDER RUNG, not preferHardwareGL.

    Deliberate 2026-07-24 (liquid_ui_service.py:1590-1602, and HOME_DESKTOP_
    DESIGN_CHECKLIST GF1): backdrop-filter blur composites ONLY on the vulkan
    rung. hart-comp forces webkit-cairo even when preferHardwareGL is on (vulkan
    demoted for the hover VK_ERROR_SURFACE_LOST_KHR), and cairo cannot paint
    blur — so keying the frost off the FLAG would render a cairo surface
    see-through. Hence: only the vulkan rung drops webkit-flat; every other rung
    keeps it opaque + legible. (Supersedes the old preferHardwareGL-keyed
    behaviour this test used to assert.)"""
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')

    # preferHardwareGL ON but the runtime rung is webkit-cairo (hart-comp's real
    # state): blur will NOT paint, so the glass MUST stay flat/opaque.
    monkeypatch.setenv('LIQUID_UI_PREFER_HW_GL', '1')
    monkeypatch.setattr(lus, 'read_shell_render_mode', lambda: 'webkit-cairo')
    body = _body_tag(svc.render_desktop_shell())
    assert 'webkit-flat' in body, (
        "a non-vulkan rung (cairo) cannot composite backdrop-filter — the glass "
        "must stay opaque even with preferHardwareGL on, or panels read see-through")

    # The vulkan rung DOES composite blur → the frosted glass returns, no flat.
    monkeypatch.setattr(lus, 'read_shell_render_mode', lambda: 'vulkan')
    body = _body_tag(svc.render_desktop_shell())
    assert 'webkit-flat' not in body, (
        "the vulkan rung composites backdrop-filter — the glass frosts, no flat")


def test_css_floor_solidifies_glass_and_panels_on_webkit_flat(svc):
    """The opaque fallback must fire on body.webkit-flat too (not only
    body.gpu-software): a hardware-probe box with compositing off must still get a
    near-opaque .glass/.panel fill with blur killed, or the floating windows are
    see-through. ONE rule, two triggers — proven on the live served stylesheet."""
    app = svc._create_flask_app()
    app.testing = True
    client = app.test_client()
    css = client.get('/shell/static/hartResponsive.css').get_data(as_text=True)
    # The glass + the floating panel window are both covered on the flat path.
    assert 'body.webkit-flat .glass' in css
    assert 'body.webkit-flat .panel' in css
    # The webkit-flat selector leads into a block that BOTH kills the (uncomposited)
    # blur AND sets a near-opaque fill — that pairing is what makes the panel solid.
    m = re.search(r'body\.webkit-flat[^{]*\{([^}]*)\}', css)
    assert m, "no body.webkit-flat rule block found on the served stylesheet"
    block = m.group(1)
    assert re.search(r'backdrop-filter:\s*none', block), \
        "webkit-flat glass must disable backdrop-filter (it paints nothing uncomposited)"
    # The fill must be an EXPLICIT opaque surface. GF1 (2026-07-12) upgraded the
    # flat grey `background: rgba(...)` to a brand-tinted `linear-gradient` (keep
    # COLOUR depth on the software floor at zero per-frame cost), so accept
    # either — but require a near-opaque alpha (>= ~0.9), which is what actually
    # stops the panel reading see-through.
    assert re.search(r'background:\s*(rgba|linear-gradient)', block), \
        "webkit-flat glass must set an explicit fill (flat rgba or brand gradient)"
    assert re.search(r'0\.9[0-9]', block), \
        "the webkit-flat fill must be near-opaque (>= ~0.9 alpha), not a see-through overlay"


# ── #138 terminal exec re-render guard (inline JS — source guard) ──────────────

def test_source_guard_terminal_exec_rerender_safe(svc):
    """The terminal panel must NOT abort/recreate an in-flight exec on a panel
    re-render: loadTerminalPanel is idempotent (bails if #term-output exists) and
    termExec ignores re-entrant calls while busy. Inline shell JS has no Python
    entry point, so this is a labelled source guard, not the only test for the
    change set (#137 above is behavioural)."""
    html = svc.render_desktop_shell()
    # Idempotent mount: re-render bails when the terminal is already live.
    assert "el.querySelector('#term-output')) return" in html
    # Re-entrancy guard: a second exec while busy is a no-op (no 2nd controller).
    assert 'if(window._hartTermBusy) return' in html
    assert 'window._hartTermBusy = true' in html
