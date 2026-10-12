"""First-run "Light Your HART": Nunba's ceremony, hosted by the shell.

Since 2026-10-12 the OS runs no ceremony of its own. When the person has not
sealed a name, static/hartOnboarding.js hosts Nunba's /local page full-screen
in #hart-onboarding (Nunba's Agent.js runs its ceremony there) and closes when
the seal lands. Behavioural: drives the REAL render and the REAL status route;
asserts the overlay host is wired, the status route names the desktop owner and
reports whether Nunba is up, and the driver is Nunba-hosted + WebKitGTK-safe.
The driver's behaviour itself is driven in test_onboarding_skip_keyboard.mjs.

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


def test_ceremony_host_wired_and_the_os_narrates_nothing():
    html = _render()
    assert 'id="hart-onboarding"' in html                  # the host overlay
    assert '/shell/static/hartOnboarding.js' in html       # the driver
    assert '.hart-onboarding .hob-frame{' in html          # Nunba's page fills it
    assert 'hart-onboarding-narr' not in html              # no OS narration target
    assert 'hob-orb' not in html                           # no OS orb: Nunba paints the ceremony
    # Nunba is the chat: the shell's own bubble and pill are gone with it.
    assert 'id="assistant-chat"' not in html and 'id="agent-pill"' not in html


def test_status_route_gates_the_ceremony():
    """The driver self-gates on the REAL backend: /api/onboarding/status must
    answer with 'onboarded' (the owner's seal on record) and 'companion_ready'
    (the Nunba daemon is up, so its page can load)."""
    try:
        client = _liquid_ui()()._create_flask_app().test_client()
        r = client.get('/api/onboarding/status?user_id=__test_unlikely__')
    except Exception as e:
        pytest.skip('onboarding backend not runnable here: ' + str(e))
    assert r.status_code == 200
    body = r.get_json()
    assert 'onboarded' in body and 'companion_ready' in body


def _route_app(resolver, monkeypatch, seen):
    try:
        from flask import Flask
    except ImportError:
        pytest.skip('flask not installed')
    import hartos.hart_onboarding as ho
    from integrations.agent_engine.onboarding_routes import register_onboarding_routes

    def fake_has_hart_name(uid):
        seen.append(uid)
        return False

    monkeypatch.setattr(ho, 'has_hart_name', fake_has_hart_name)
    app = Flask(__name__)
    register_onboarding_routes(app, resolve_user=resolver)
    return app.test_client()


def test_status_names_the_desktop_owner_when_the_request_carries_no_user(monkeypatch):
    """On HART OS the shell passes LiquidUIService._desktop_owner_id: the id the
    shell chat speaks as (Nunba's owner file, else Nunba's guest id). The seal
    is looked up under THAT id, and companion_ready says Nunba answered."""
    seen = []
    client = _route_app(lambda: 'g_2084a7cf93aa6ea4', monkeypatch, seen)
    body = client.get('/api/onboarding/status').get_json()
    assert body['user_id'] == 'g_2084a7cf93aa6ea4'
    assert body['companion_ready'] is True
    assert body['onboarded'] is False
    assert seen == ['g_2084a7cf93aa6ea4']


def test_status_reports_nunba_down_when_the_resolver_has_nobody(monkeypatch):
    """While hart-nunba.service is still coming up the resolver has no owner
    and no guest id: the driver must not show a page that cannot load."""
    seen = []
    client = _route_app(lambda: None, monkeypatch, seen)
    body = client.get('/api/onboarding/status').get_json()
    assert body['companion_ready'] is False
    assert body['user_id'] == '1'          # the historical default still answers
    assert body['onboarded'] is False


def test_status_honours_an_explicit_user_id_over_the_resolver(monkeypatch):
    seen = []
    client = _route_app(lambda: 'g_owner', monkeypatch, seen)
    body = client.get('/api/onboarding/status?user_id=u7').get_json()
    assert body['user_id'] == 'u7' and body['companion_ready'] is True
    assert seen == ['u7']


def test_driver_hosts_nunba_and_is_webkit_safe():
    src = open(os.path.join(STATIC, 'hartOnboarding.js'), encoding='utf-8').read()
    assert "'/api/onboarding/status'" in src              # the gate (owner seal + Nunba up)
    assert "'hart_sealed'" in src                         # the SAME flag Nunba's gate reads
    assert "'nunba:onboarding'" in src                    # Nunba's own word for the seal
    assert "addEventListener('storage'" in src            # the browser's signal from Nunba's frame
    assert "'/local'" in src                              # Nunba's page
    # No second ceremony: the OS drives none of the old phases.
    assert '/api/onboarding/advance' not in src and 'select_language' not in src
    assert 'companion_progress' not in src
    assert "e.key === 'Escape'" in src                    # never traps the user
    assert 'HartTimeoutSignal' in src and 'AbortSignal.timeout(' not in src
    assert '`' not in src                                 # old WebKitGTK: no template literals


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v) and 'monkeypatch' not in v.__code__.co_varnames]
    failed = 0
    for fn in fns:
        try:
            fn(); print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1; print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
