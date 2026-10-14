"""N7 gate 1: a turn typed in the HART OS shell goes to the Nunba daemon, as the owner.

Measured 2026-10-11 on the Samsung node (generation 15): LiquidUI's
/api/agent/ask posted to the backend's /chat as a fixed 'hart_desktop_user'
with prompt_id 'desktop_agent', while the preinstalled Nunba daemon served the
same backend behind its own /chat on unix:/run/hart/nunba.sock (its /health
answered 200 with llama_port 808, its API 401 without a Bearer). These pin the
route's new shape with a fake socket client: the request goes to Nunba's /chat
with a Bearer and the Host the proxy uses, as the owner from Nunba's own
user_data.json when present, else Nunba's guest id; the reply's text reaches
the bubble under 'response'; a daemon that is down falls back to the backend.
"""
import json
import os
from unittest.mock import patch

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSocketClient:
    def __init__(self, chat_payload=None, status=200, guest='guest-abc'):
        self.calls = []
        self.chat_payload = chat_payload if chat_payload is not None else {'text': 'hello from nunba'}
        self.status = status
        self.guest = guest

    def get(self, url, headers=None, timeout=None):
        self.calls.append(('GET', url, headers))
        if url.endswith('/api/guest-id'):
            return _Resp(200, {'guest_id': self.guest})
        return _Resp(404, {})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(('POST', url, headers, json))
        return _Resp(self.status, dict(self.chat_payload))


@pytest.fixture(autouse=True)
def _not_halted():
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted', return_value=False):
        yield


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setattr('integrations.social.auth.generate_jwt', lambda user_id, username, role='flat', tenant_id=None: 'tok-' + user_id)
    s = LiquidUIService(a2ui_enabled=False)
    s._nunba_server_name = 'Nunba'
    monkeypatch.setattr(s, '_compose_intent_result', lambda text, payload: False)
    return s


def _wire(svc, client):
    svc._nunba_chat_client = lambda: client
    return svc._create_flask_app().test_client()


def test_the_turn_goes_to_nunba_as_the_owner_with_a_bearer_and_the_proxy_host(svc, tmp_path):
    owner = tmp_path / 'Documents' / 'HevolveAi Agent Companion' / 'storage'
    owner.mkdir(parents=True)
    (owner / 'user_data.json').write_text(json.dumps({'user_id': 'owner-42', 'email': 'o@x'}), encoding='utf-8')
    fake = _FakeSocketClient({'response': 'hi owner', 'intent': 'casual'})
    tc = _wire(svc, fake)
    r = tc.post('/api/agent/ask', json={'text': 'hi'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['response'] == 'hi owner'
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert len(posts) == 1
    _, url, headers, payload = posts[0]
    assert url == 'http://nunba/chat'
    assert headers['Authorization'] == 'Bearer tok-owner-42'
    assert headers['Host'] == 'Nunba'
    assert payload['user_id'] == 'owner-42'
    assert payload['text'] == 'hi'
    assert payload['agent_type'] == 'local' and payload['agent_id'] == 'local_assistant'
    from core.chat_client import is_user_turn
    assert is_user_turn(payload), 'the id is minted by normalize_chat_body as a USER turn, never daemon_-prefixed'
    assert not [c for c in fake.calls if c[0] == 'GET'], 'with an owner file there is no guest-id lookup'


def test_without_a_signed_in_owner_the_shell_speaks_as_nunbas_guest_id(svc):
    fake = _FakeSocketClient({'text': 'hello guest'}, guest='hw-guest-7')
    tc = _wire(svc, fake)
    r = tc.post('/api/agent/ask', json={'text': 'hi'})
    assert r.get_json()['response'] == 'hello guest', 'a text-only reply is surfaced under response for the bubble'
    post = [c for c in fake.calls if c[0] == 'POST'][0]
    assert post[3]['user_id'] == 'hw-guest-7'
    assert post[2]['Authorization'] == 'Bearer tok-hw-guest-7'
    gets = [c for c in fake.calls if c[0] == 'GET']
    assert gets and gets[0][1].endswith('/api/guest-id')
    tc.post('/api/agent/ask', json={'text': 'again'})
    assert len([c for c in fake.calls if c[0] == 'GET']) == 1, 'the guest id is hardware-stable and cached'


def test_a_daemon_that_is_down_falls_back_to_the_backend_path(svc, monkeypatch):
    class _Down:
        def get(self, *a, **k):
            raise ConnectionError('socket down')

        def post(self, *a, **k):
            raise ConnectionError('socket down')

    seen = {}

    class _R:
        def json(self):
            return {'response': 'backend said hi'}

    def _post_chat(url, body, **kw):
        seen['url'] = url
        seen['body'] = body
        return _R()

    monkeypatch.setattr('core.chat_client.post_chat', _post_chat)
    tc = _wire(svc, _Down())
    r = tc.post('/api/agent/ask', json={'text': 'hi'})
    assert r.get_json()['response'] == 'backend said hi'
    assert seen['url'].endswith('/chat') and seen['body']['prompt'] == 'hi'


def test_no_socket_configured_means_the_old_backend_path_exactly(svc, monkeypatch):
    seen = {}

    class _R:
        def json(self):
            return {'response': 'backend'}

    monkeypatch.setattr('core.chat_client.post_chat', lambda url, body, **kw: seen.setdefault('body', body) and _R() or _R())
    assert not hasattr(svc, '_nunba_chat_client')
    tc = svc._create_flask_app().test_client()
    r = tc.post('/api/agent/ask', json={'text': 'hi'})
    assert r.get_json()['response'] == 'backend'
    assert seen['body']['user_id'] == 'hart_desktop_user'
