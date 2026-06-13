"""First-run "Light Your HART" ceremony (web, in-shell).

After OS install, when not onboarded, the shell auto-runs the narrated phased
ceremony as a full-screen overlay, driving the EXISTING /api/onboarding backend
(no GTK4 process => no software-GL risk on the cage kiosk). Behavioural: drives
the REAL render + the onboarding status route; asserts the overlay is wired and
the driver reuses the real ceremony API + is WebKitGTK-safe.

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


def _render():
    return _liquid_ui()().render_desktop_shell()


def test_ceremony_overlay_and_driver_wired():
    html = _render()
    assert 'id="hart-onboarding"' in html                  # the overlay
    assert 'id="hart-onboarding-narr"' in html             # narration target
    assert '/shell/static/hartOnboarding.js' in html       # the driver
    assert '.hart-onboarding .hob-orb{' in html            # ceremony stylesheet


def test_status_route_gates_the_ceremony():
    """The ceremony self-gates on the REAL backend: /api/onboarding/status must
    answer with an 'onboarded' flag (the driver skips when already lit)."""
    try:
        client = _liquid_ui()()._create_flask_app().test_client()
        r = client.get('/api/onboarding/status?user_id=__test_unlikely__')
    except Exception as e:
        pytest.skip('onboarding backend not runnable here: ' + str(e))
    assert r.status_code == 200
    assert 'onboarded' in r.get_json()


def test_driver_uses_real_ceremony_api_and_is_webkit_safe():
    src = open(os.path.join(STATIC, 'hartOnboarding.js'), encoding='utf-8').read()
    for ep in ("'/api/onboarding/status", "'/api/onboarding/start'", "'/api/onboarding/advance'"):
        assert ep in src, ep
    assert 'select_language' in src and 'accept_name' in src   # drives the phases
    assert 'window.speakText' in src                           # PA voice (reuse)
    assert "e.key === 'Escape'" in src                         # never traps the user
    assert 'HartTimeoutSignal' in src and 'AbortSignal.timeout(' not in src


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
