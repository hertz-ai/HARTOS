"""A paid call never reaches the /chat that ignores the relay's contract.

On the installed Nunba desktop the Flask port (5000) serves Nunba's own chat
route (routes/chatbot_routes.py ``_chat_turn``), not HARTOS's /chat.  That
route reads none of the flags the relay sends (``agent_tools``, ``lane``,
``draft_first``) and picks the casual tool set itself.  The review of
d78c43eae measured it live on 2026-10-29: the relay's exact body with a
guest's credential ran casual=True with 40 tools (Shell_Command and
Computer_Action among them), came back 200 ``success: true`` source
``langchain_local``, and was billed.  The main model answered it (the 4B
server logged the request at 20:49:31); /chat's own log line "routing to
draft 0.8B" is a fixed label, and no draft was enabled at that boot.  d78c43eae had
pointed the relay at that port (task #160); this guards its revert.

The relay dials the backend port only.  There HARTOS's own /chat runs the
turn under the contract (tests/unit/test_metered_chat_is_a_conversation.py
pins the flags that turn runs with); on the desktop nothing listens there, so
the customer gets a 503 and is not billed.

Behavioural: the real metered endpoint.  The port probe, the transport and
the key checks are the stubbed boundaries; the transport models the desktop
as measured (the backend port refuses, the Flask port answers with Nunba's
reply body).
"""
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import requests
from flask import Flask

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Nunba's reply to a turn it answered (routes/chatbot_routes.py, the
# langchain_local branch).
NUNBAS_REPLY = {'text': 'Hi! How can I help you today?', 'agent_type': 'local',
                'source': 'langchain_local', 'success': True}
HARTOS_ANSWER = 'Paris is the capital of France.'


def _paid_call(monkeypatch, serving):
    """POST /api/v1/intelligence/chat with a free key on a node whose loopback
    serves only ``serving`` ({service name: its /chat reply body}).  Returns
    (the customer's response, the log_usage mock, the URLs dialled)."""
    import core.port_registry as pr
    from integrations.agent_engine.commercial_api import (
        CommercialAPIService, commercial_api_bp)

    bodies = {pr.get_port(s): body for s, body in serving.items()}
    monkeypatch.setattr(pr, '_is_port_listening',
                        lambda port, *a, **k: int(port) in bodies)
    dialled = []

    def transport(url, json=None, headers=None, timeout=None, **_):
        dialled.append(url)
        port = urlsplit(url).port
        if port not in bodies:
            raise requests.ConnectionError(f'nothing listens on {port}')
        out = MagicMock()
        out.status_code = 200
        out.json.return_value = dict(bodies[port])
        return out

    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    key_data = {'id': 'key-1', 'user_id': 'guest-7', 'tier': 'free'}
    with patch('integrations.social.models.get_db', return_value=MagicMock()), \
            patch.object(CommercialAPIService, 'validate_api_key', return_value=key_data), \
            patch.object(CommercialAPIService, 'check_rate_limit', return_value=True), \
            patch.object(CommercialAPIService, 'reserve_quota', return_value=True), \
            patch.object(CommercialAPIService, 'log_usage') as log_usage, \
            patch('core.http_pool.pooled_post', side_effect=transport):
        r = app.test_client().post(
            '/api/v1/intelligence/chat', json={'prompt': 'hi'},
            headers={'X-API-Key': 'customer-key'})
    return r, log_usage, dialled


def test_on_the_bundled_desktop_a_paid_call_fails_closed_unbilled(monkeypatch):
    import core.port_registry as pr
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)

    r, log_usage, dialled = _paid_call(monkeypatch, {'flask': NUNBAS_REPLY})

    assert dialled, 'the relay never sent the turn'
    assert pr.get_port('flask') not in {urlsplit(u).port for u in dialled}, (
        "the paid turn went to Nunba's /chat, which ignores the contract: %r"
        % dialled)
    payload = r.get_json()
    assert r.status_code == 503, payload
    assert payload.get('success') is False, payload
    assert payload.get('response') is None, payload
    assert log_usage.called, 'the failed call is still logged for audit'
    kwargs = log_usage.call_args.kwargs
    assert kwargs.get('status_code') not in (None, 200), kwargs
    billable = (kwargs.get('tokens_in', 0) or 0) + (kwargs.get('tokens_out', 0) or 0)
    assert billable == 0, 'customer billed %d tokens for no answer' % billable


def test_on_central_a_paid_call_is_answered_by_its_own_chat_on_loopback(monkeypatch):
    import core.port_registry as pr
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    # Central's own setting, measured 2026-10-29: its LAN address on :6777.
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://192.168.0.9:6777')

    r, log_usage, dialled = _paid_call(
        monkeypatch, {'backend': {'response': HARTOS_ANSWER}})

    assert dialled == [f'http://localhost:{pr.get_port("backend")}/chat'], dialled
    assert r.status_code == 200, r.get_json()
    assert r.get_json().get('response') == HARTOS_ANSWER
    assert log_usage.call_args.kwargs.get('status_code', 200) == 200
