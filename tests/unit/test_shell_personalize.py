"""Themes & Wallpaper gallery (Phase B) + the shared session layer.

Drives the REAL render_desktop_shell() for the panel delegate + gallery CSS +
script load-order, and asserts the personalize/session JS modules expose their
contracts and reuse the shell's primitives (applyPreset, the wallpaper routes,
HartSession) rather than forking them. The session-state round-trip behaviour is
covered behaviourally in test_shell_desktop_icons.

Local note: this box OOMs pytest; run the inline runner at the bottom. CI runs it.
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STATIC = os.path.join(ROOT, 'integrations', 'agent_engine', 'static')


def _liquid_ui():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps absent in a minimal CI runner -> skip
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def _render():
    return _liquid_ui()().render_desktop_shell()


def test_personalize_scripts_and_gallery_are_wired():
    html = _render()
    assert '/shell/static/hartSession.js' in html
    assert '/shell/static/hartPersonalize.js' in html
    assert '.hart-gallery{' in html and '.hart-tile{' in html


def test_session_loads_before_its_writers():
    """hartSession.js must load before hartDesktop.js / hartPersonalize.js, which
    call window.HartSession — otherwise first writes are dropped."""
    html = _render()
    s = html.index('/shell/static/hartSession.js')
    assert s < html.index('/shell/static/hartDesktop.js')
    assert s < html.index('/shell/static/hartPersonalize.js')


def test_wallpaper_panel_delegates_not_dead_body():
    html = _render()
    assert 'window.hartRenderPersonalize(el)' in html        # delegate
    assert "html += dsStatusRow('wallpaper', 'Current'" not in html  # old body gone


def test_desktop_menu_opens_personalize():
    html = _render()
    assert "ctxItem('palette','Personalize','openPanel(\"wallpaper_manager\")')" in html


def test_personalize_js_reuses_primitives_and_8_presets():
    src = open(os.path.join(STATIC, 'hartPersonalize.js'), encoding='utf-8').read()
    assert 'window.hartRenderPersonalize =' in src
    assert 'window.hartSetWallpaper =' in src
    assert 'window.applyPreset' in src           # reuse theme apply (no fork)
    assert 'HartSession' in src                  # shared persistence
    for pid in ('hart-default', 'cyberpunk', 'forest', 'sunset', 'arctic',
                'midnight', 'minimal', 'potato'):
        assert "'" + pid + "'" in src, pid       # all 8 server presets present


def test_session_js_is_a_single_keyed_writer():
    src = open(os.path.join(STATIC, 'hartSession.js'), encoding='utf-8').read()
    for member in ('ready:', 'get:', 'set:'):
        assert member in src, member
    assert "'/api/shell/session-state'" in src
    # Saves the WHOLE blob (merge-by-key) so modules don't clobber each other.
    assert 'JSON.stringify(blob' in src


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1; print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
