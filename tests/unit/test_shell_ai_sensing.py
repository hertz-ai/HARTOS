"""AI sensory kill-switch — the human's hard cut over what the AI can sense.

Behavioural: drives core.ai_sensing's REAL gate, the REAL Flask routes
(/api/shell/ai-sensing + /api/voice actually refusing transcription when hearing
is cut), and asserts the floating button + live-proof JS are wired, drive the
real gate, and are WebKitGTK-safe.

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
    except Exception as e:
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def _sensing():
    try:
        from core import ai_sensing
    except Exception as e:
        pytest.skip('core.ai_sensing not importable: ' + str(e))
    return ai_sensing


def test_gate_toggles_and_status_shape():
    s = _sensing()
    try:
        assert s.allowed('mic') is True                 # default: sensing on
        st = s.disable_all()
        assert s.allowed('mic') is False and s.any_disabled() is True
        assert st['disabled'] == {'mic': True, 'camera': True, 'screen': True}
        assert 'camera_service_running' in st['proof']  # live proof field
        assert st['sensing_enabled'] is False
        st2 = s.enable_all()
        assert s.allowed('mic') is True and st2['sensing_enabled'] is True
    finally:
        s.enable_all()


def test_voice_endpoint_refuses_when_hearing_cut():
    """The REAL mic ingestion: /api/voice must 403 when hearing is cut (no audio
    consumed), and stop refusing when woken."""
    LiquidUIService = _liquid_ui()
    s = _sensing()
    client = LiquidUIService()._create_flask_app().test_client()
    try:
        s.disable_all()
        r = client.post('/api/voice')                   # gate fires before no-audio check
        assert r.status_code == 403
        assert r.get_json().get('sensing_disabled') is True
        s.enable_all()
        r2 = client.post('/api/voice')                  # now allowed -> not gated
        assert r2.status_code != 403
    finally:
        s.enable_all()


def test_sensing_routes_roundtrip():
    """Only this human route flips the gate; status() reflects it live."""
    try:
        from flask import Flask
        from integrations.agent_engine.shell_desktop_apis import register_shell_desktop_routes
    except Exception as e:
        pytest.skip('shell desktop routes not importable: ' + str(e))
    s = _sensing()
    app = Flask(__name__)
    register_shell_desktop_routes(app)
    client = app.test_client()
    try:
        client.post('/api/shell/ai-sensing', json={'action': 'off'})
        st = client.get('/api/shell/ai-sensing').get_json()
        assert st['disabled']['mic'] is True and st['sensing_enabled'] is False
        client.post('/api/shell/ai-sensing', json={'action': 'on'})
        st2 = client.get('/api/shell/ai-sensing').get_json()
        assert st2['sensing_enabled'] is True
    finally:
        s.enable_all()


def test_killswitch_wired_into_shell():
    html = _liquid_ui()().render_desktop_shell()
    assert 'id="hart-senses-btn"' in html
    assert '/shell/static/hartSenses.js' in html
    assert '.hart-hero.ai-blind #hart-voice-orb' in html   # orb closes its eyes


def test_senses_js_drives_real_gate_and_is_webkit_safe():
    src = open(os.path.join(STATIC, 'hartSenses.js'), encoding='utf-8').read()
    assert "'/api/shell/ai-sensing'" in src                 # the real gate route
    assert 'HartTimeoutSignal' in src and 'AbortSignal.timeout(' not in src
    assert 'ai-blind' in src                                # orb darken hook


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
