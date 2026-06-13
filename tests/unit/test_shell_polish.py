"""Buttery polish + speed (Phase D): window spring-open + dock magnification.

Drives the REAL render_desktop_shell() for the entrance animation + dock script
wiring, and asserts hartDock.js magnifies the taskbar with GPU transforms,
rAF-throttled, and skips the low-end tier.

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


def test_window_spring_open_and_dock_wired():
    html = _render()
    assert '@keyframes hart-panel-in{' in html
    assert '.panel{animation:hart-panel-in' in html
    assert '/shell/static/hartDock.js' in html


def test_spring_open_respects_reduced_motion():
    html = _render()
    assert 'html.a11y-rmotion .panel{animation:none}' in html


def test_dock_js_magnifies_taskbar_and_skips_potato():
    src = open(os.path.join(STATIC, 'hartDock.js'), encoding='utf-8').read()
    assert "getElementById('taskbar')" in src
    assert '.taskbar-chip' in src
    assert "c.style.transform = 'scale(" in src       # GPU transform magnify
    assert 'requestAnimationFrame' in src             # rAF-throttled
    assert 'PERF.potato' in src                       # skip on low-end tier


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
