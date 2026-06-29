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
    monkeypatch.setattr(lus, 'read_gpu_render_mode', lambda: 'hardware')
    body = _body_tag(svc.render_desktop_shell())
    assert 'gpu-hardware' in body
    assert 'gpu-software' not in body


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
