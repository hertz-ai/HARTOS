"""A2A message/send through central's gate while a Hevolve sign-in check is
paced (kong_identity.KongBusy).

Review rv_4e62 (hartos-52) of 4e6245757: the gate answered the paced check
as a 3-tuple (body, 503, headers).  A2A's admission
(google_a2a_integration._admit_checks) unpacks the gate's refusal as
``resp, status``, so it raised ValueError: a 500 "too many values to unpack"
and an ERROR log, where the parent answered 401.  The gate's answer is the
(response, status) pair every other refusal of it is, with Retry-After on
the response.
"""
import logging
import os
import sys
import types
import uuid
from unittest.mock import patch

import pytest
from flask import Flask

import integrations.google_a2a.peer_reuse as peer_reuse
from integrations.google_a2a.google_a2a_integration import A2AProtocolServer
from integrations.social import kong_identity
from integrations.social.models import Base, get_engine
from security.middleware import _apply_api_auth

AGENT = 'busygate_shared_0'


@pytest.fixture
def central(monkeypatch):
    """A central node's gate in front of its A2A jsonrpc view."""
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'NUNBA_CI',
                 'TRUSTED_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    Base.metadata.create_all(get_engine())
    monkeypatch.setattr(peer_reuse, 'export_allowed', lambda pid: True)
    app = Flask('busy_gate_a2a')
    app.config['PROPAGATE_EXCEPTIONS'] = False
    _apply_api_auth(app)
    srv = A2AProtocolServer(app, 'http://node-a')
    ran = []

    async def spy(text, ctx):
        ran.append(text)
        return {'role': 'model', 'parts': [{'text': f'ran:{text}'}]}
    srv.register_agent(AGENT, 'shared', 'd', [{'id': 's'}], spy)
    srv.setup_routes()
    secrets = {'security.secrets_manager': types.SimpleNamespace(
        get_secret=lambda name: os.environ.get(name, ''))}
    with patch.dict(sys.modules, secrets):
        yield types.SimpleNamespace(client=app.test_client(), ran=ran)


def _send(client):
    body = {'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'message/send',
            'params': {'message': {'messageId': uuid.uuid4().hex,
                                   'parts': [{'kind': 'text', 'text': 'hi'}]}}}
    return client.post(f'/a2a/{AGENT}/jsonrpc', json=body,
                       headers={'Authorization': 'Bearer kong-token-busy'},
                       environ_base={'REMOTE_ADDR': '198.51.100.23'})


def test_a2a_through_a_paced_sign_in_check_says_busy(central, caplog):
    with (caplog.at_level(logging.ERROR),
          patch.object(kong_identity, 'email_for_token',
                       side_effect=kong_identity.KongBusy(20))):
        resp = _send(central.client)
    assert resp.status_code == 503, (resp.status_code,
                                     resp.get_data(as_text=True)[:300])
    error = (resp.get_json() or {}).get('error') or {}
    assert error.get('message') == kong_identity.KongBusy(20).message
    assert central.ran == [], 'a paced check ran the agent'
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        [r.getMessage() for r in caplog.records][:3]


def test_the_gates_busy_answer_is_a_response_and_a_status(monkeypatch):
    """Every caller that unpacks the gate's refusal gets (response, status),
    with Retry-After on the response."""
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    Base.metadata.create_all(get_engine())
    app = Flask('busy_gate_shape')
    check = _apply_api_auth(app, register=False)
    secrets = {'security.secrets_manager': types.SimpleNamespace(
        get_secret=lambda name: os.environ.get(name, ''))}
    with (patch.dict(sys.modules, secrets),
          patch.object(kong_identity, 'email_for_token',
                       side_effect=kong_identity.KongBusy(20)),
          app.test_request_context(
              '/chat', headers={'Authorization': 'Bearer kong-token-busy'},
              environ_base={'REMOTE_ADDR': '198.51.100.23'})):
        answer = check(as_path='/chat')
    assert isinstance(answer, tuple) and len(answer) == 2, answer
    resp, status = answer
    assert status == 503
    assert resp.headers.get('Retry-After') == '20'
    sentence = kong_identity.KongBusy(20).message
    # `message` too: the phone's custom-bot chat shows `message` from the
    # body (CustomBotChatScreen.js), and showed "OK" without it.
    assert resp.get_json() == {'error': sentence, 'message': sentence,
                               'code': 'login_check_busy'}
