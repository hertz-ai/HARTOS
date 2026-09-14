"""A desktop's socket takes callers from other machines only with a credential.

Measured 2026-09-14 on an installed desktop: Nunba's app serves the whole API
on 0.0.0.0:5000, the address the desktop advertises to peers, and a device on
the same Wi-Fi could drive /chat and read /prompts with no credential.
HARTOS's API gate lived only on hart_intelligence_entry's own app, and on a
bundled node it returned at once for every caller.  Now
security.middleware.install_api_gate puts it on the host app (bootstrap calls
it first), and on a bundled node the gate trusts this machine's own callers
(the SPA, the tray, in-process test clients: 127.0.0.1), lets another machine
reach the exempt paths that carry the peer protocol, and asks everyone else
for a credential.  Other tiers are unchanged.  The gate is never left open:
when Flask will not take the hook, it goes where the decorator puts it.

    python -m pytest tests/unit/test_desktop_socket_gate.py --noconftest -q
"""
import logging
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from security.middleware import _apply_api_auth, install_api_gate  # noqa: E402

LAN = {'REMOTE_ADDR': '192.168.0.50'}
LOOPBACK = {'REMOTE_ADDR': '127.0.0.1'}
GATED = ['/chat', '/prompts', '/api/nunba/settings', '/api/admin/ping']
PEER = ['/api/social/peers/announce', '/api/social/peers/health',
        '/api/social/federation/inbox', '/status', '/a2a/agents',
        '/.well-known/agent.json']

# The steps _run_bootstrap runs inside its setup-lock window, stubbed so the
# wiring can be driven without a real boot.
_BOOT_STEPS = ('_init_social_subsystem', '_register_core_blueprints',
               '_run_consumer_hook', '_register_hive_blueprints',
               '_init_a2a_server', '_init_database', '_init_channel_adapters',
               '_init_hevolveai_subprocess', '_init_agent_engine_subsystem',
               '_init_livekit_supervisor', '_init_whatsapp_supervisor',
               '_run_on_bootstrap_complete')


def _app():
    app = Flask('desktop_socket')
    for i, path in enumerate(GATED + PEER):
        app.add_url_rule(path, f'r{i}', lambda: {'ok': True},
                         methods=['GET', 'POST'])
    return app


def _secrets(key=''):
    """security.secrets_manager answering HEVOLVE_API_KEY with ``key``."""
    return {'security.secrets_manager': types.SimpleNamespace(
        get_secret=lambda name: key)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def desktop(env):
    app = _app()
    _apply_api_auth(app)
    with patch.dict(sys.modules, _secrets()):
        yield app.test_client()


@pytest.mark.parametrize('path', GATED)
def test_another_machine_needs_a_credential(desktop, path):
    assert desktop.get(path, environ_base=LAN).status_code == 401
    assert desktop.post(path, environ_base=LAN).status_code == 401


@pytest.mark.parametrize('path', PEER)
def test_another_machine_still_reaches_the_peer_protocol(desktop, path):
    assert desktop.get(path, environ_base=LAN).status_code == 200


@pytest.mark.parametrize('path', GATED + PEER)
def test_this_machine_is_trusted_as_before(desktop, path):
    assert desktop.get(path, environ_base=LOOPBACK).status_code == 200


def test_a_valid_token_lets_another_machine_in(desktop):
    with patch('integrations.social.auth.decode_jwt', return_value={'user_id': 'u1'}):
        r = desktop.post('/chat', environ_base=LAN,
                         headers={'Authorization': 'Bearer good'})
    assert r.status_code == 200


def test_a_bad_token_does_not(desktop):
    with patch('integrations.social.auth.decode_jwt', return_value=None):
        r = desktop.post('/chat', environ_base=LAN,
                         headers={'Authorization': 'Bearer forged'})
    assert r.status_code == 401


def test_a_forwarded_header_does_not_make_a_lan_caller_local(desktop):
    r = desktop.get('/chat', environ_base=LAN,
                    headers={'X-Forwarded-For': '127.0.0.1', 'Host': 'localhost'})
    assert r.status_code == 401


@pytest.mark.parametrize('bundled, tier', [(True, 'flat'), (False, 'central')])
def test_both_branches_accept_the_same_key(monkeypatch, bundled, tier):
    if bundled:
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
    else:
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    app = _app()
    _apply_api_auth(app)
    client = app.test_client()
    with patch.dict(sys.modules, _secrets('k-123')):
        ok = client.get('/prompts', environ_base=LAN, headers={'X-API-Key': 'k-123'})
        wrong = client.get('/prompts', environ_base=LAN, headers={'X-API-Key': 'nope'})
    assert ok.status_code == 200
    assert wrong.status_code == 401


def test_other_tiers_are_unchanged(monkeypatch):
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    app = _app()
    _apply_api_auth(app)
    client = app.test_client()
    with patch.dict(sys.modules, _secrets()):
        assert client.post('/chat', environ_base=LAN).status_code == 200
        assert client.get('/api/nunba/settings', environ_base=LAN).status_code == 200
        assert client.get('/api/admin/ping', environ_base=LAN).status_code == 401


def test_bootstrap_gates_an_app_that_is_already_serving(env):
    import hartos.hartos_bootstrap as hb
    app = _app()
    client = app.test_client()
    with patch.dict(sys.modules, _secrets()):
        assert client.get('/chat', environ_base=LAN).status_code == 200  # no gate yet
        env.setattr(hb, '_BOOTSTRAP_DONE', hb._BOOTSTRAP_DONE)
        with patch.multiple(hb, **{step: MagicMock() for step in _BOOT_STEPS}):
            hb._run_bootstrap(app, {})
        assert client.get('/chat', environ_base=LAN).status_code == 401
        assert client.get('/chat', environ_base=LOOPBACK).status_code == 200
        assert client.get('/api/social/peers/health',
                          environ_base=LAN).status_code == 200


def test_boot_goes_on_when_the_gate_cannot_be_installed(env, caplog):
    import hartos.hartos_bootstrap as hb
    env.setattr(hb, '_BOOTSTRAP_DONE', hb._BOOTSTRAP_DONE)
    steps = {step: MagicMock() for step in _BOOT_STEPS}
    with patch('security.middleware.install_api_gate',
               side_effect=RuntimeError('no gate')), \
            patch.multiple(hb, **steps), \
            caplog.at_level(logging.CRITICAL, logger=hb.__name__):
        hb._run_bootstrap(_app(), {})
    assert any(r.levelno == logging.CRITICAL and 'gate' in r.getMessage()
               for r in caplog.records)
    assert all(stub.called for stub in steps.values()), 'the boot stopped at the gate'


def test_the_gate_is_never_left_open_when_flask_refuses_the_hook(env, caplog):
    app = _app()
    client = app.test_client()
    with patch.dict(sys.modules, _secrets()):
        # Served once, outside any setup-lock window: Flask now refuses a
        # before_request hook, the failure the fallback exists for.
        assert client.get('/chat', environ_base=LAN).status_code == 200
        with caplog.at_level(logging.CRITICAL, logger='hevolve_security'):
            assert install_api_gate(app) is True
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        assert app._hartos_api_gate is True
        assert client.get('/chat', environ_base=LAN).status_code == 401
        assert client.get('/chat', environ_base=LOOPBACK).status_code == 200
        assert client.get('/api/social/peers/health',
                          environ_base=LAN).status_code == 200


def test_the_gate_goes_on_once(env):
    app = _app()
    assert install_api_gate(app) is True
    hooks = len(app.before_request_funcs[None])
    assert install_api_gate(app) is True
    assert len(app.before_request_funcs[None]) == hooks


def test_the_staging_container_lets_its_probe_through(desktop, monkeypatch):
    """Nunba's staging e2e (NUNBA_CI=1, docker-compose.staging.yml) probes
    through Docker's port mapping.  core.auth_local trusts every caller
    there, as Nunba's own rule does, so the gate lets the probe through.
    Measured 2026-09-14: without this, /backend/health answered the probe
    401 on every poll and the staging e2e failed."""
    monkeypatch.setenv('NUNBA_CI', '1')
    assert desktop.get('/chat', environ_base=LAN).status_code == 200
    monkeypatch.delenv('NUNBA_CI')
    assert desktop.get('/chat', environ_base=LAN).status_code == 401, \
        'NUNBA_CI is the only thing that opens it'


def test_installing_the_gate_under_nunba_ci_says_so(env, caplog):
    """A production node that set NUNBA_CI would trust every caller, so the
    gate says so at CRITICAL when it goes on under it."""
    env.setenv('NUNBA_CI', '1')
    with caplog.at_level(logging.CRITICAL, logger='hevolve_security'):
        assert install_api_gate(_app()) is True
    assert any('NUNBA_CI=1' in r.getMessage() for r in caplog.records
               if r.levelno == logging.CRITICAL)
