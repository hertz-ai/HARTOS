"""The metered /v1/intelligence/* endpoints self-POST to this node's own
/chat.  They were the 4th and 5th instances of the cdd379ad defect.

cdd379ad fixed the two speculative-dispatcher legs, 272c4315 called itself
"the third instance" (worker_loop), and 503d8e98 consolidated them.  All
three caller audits missed these two, which are the *billed* ones:

  * no credential -> security/middleware.py gate 2 answers a bare self-POST
    with 401 on the central/regional tiers,
  * ``pooled_post`` does NOT ``raise_for_status()`` (core/http_pool.py), so
    the 401 never raised, the ``except`` never fired, and nothing was logged,
  * ``log_usage`` then ran unconditionally with the default
    ``status_code=200``, so the customer was BILLED for ``tokens_in`` and
    handed ``{'success': True, 'response': ''}``.

The no-bill-on-failure half is not a new policy: ``require_api_key`` already
states it for the 429 path -- "No log_usage() is called ... the backend was
never invoked, so no inference tokens are recorded."
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


MINTED = {'X-API-Key': 'node-key'}


@pytest.fixture
def app_client():
    """Real blueprint, real endpoint code; only the auth gate is stubbed."""
    from integrations.agent_engine.commercial_api import commercial_api_bp

    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with app.test_client() as client:
        yield client


@pytest.fixture
def passing_auth():
    """Let require_api_key through without a DB, leaving the endpoint real."""
    from integrations.agent_engine.commercial_api import CommercialAPIService

    db = MagicMock()
    key_data = {'id': 'key-1', 'user_id': 'cust-42', 'tier': 'pro'}
    with patch('integrations.social.models.get_db', return_value=db), \
            patch.object(CommercialAPIService, 'validate_api_key',
                         return_value=key_data), \
            patch.object(CommercialAPIService, 'check_rate_limit',
                         return_value=True), \
            patch.object(CommercialAPIService, 'reserve_quota',
                         return_value=True), \
            patch.object(CommercialAPIService, 'log_usage') as log_usage:
        yield log_usage


def _resp(status_code, payload):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


@pytest.mark.parametrize('route,body,prompt_id,resp_key', [
    ('/api/v1/intelligence/chat', {'prompt': 'hello'},
     'api_intelligence', 'response'),
    ('/api/v1/intelligence/analyze',
     {'document': 'some doc', 'question': 'what?'}, 'api_analyze', 'analysis'),
])
def test_metered_chat_self_post_carries_daemon_credential(
        app_client, passing_auth, route, body, prompt_id, resp_key):
    """Both metered endpoints send the credential dispatch._internal_auth_headers
    mints.  Bare, they are answered 401 by gate 2 on central/regional."""
    captured = {}

    def fake_post(url, **kwargs):
        captured['url'] = url
        captured['headers'] = kwargs.get('headers')
        captured['json'] = kwargs.get('json')
        return _resp(200, {'response': 'the answer'})

    with patch('core.http_pool.pooled_post', side_effect=fake_post), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        r = app_client.post(route, json=body, headers={'X-API-Key': 'k'})

    assert r.status_code == 200
    assert captured['headers'] == MINTED, (
        'metered self-POST went out bare; gate 2 answers 401 on central')
    # HARTOS dialect preserved -- the backend reads `prompt`, not `text`.
    assert captured['json']['prompt']
    assert captured['json']['prompt_id'] == prompt_id
    assert r.get_json()[resp_key] == 'the answer'


@pytest.mark.parametrize('route,body', [
    ('/api/v1/intelligence/chat', {'prompt': 'hello'}),
    ('/api/v1/intelligence/analyze', {'document': 'd', 'question': 'q'}),
])
def test_backend_401_is_not_billed_and_not_reported_as_success(
        app_client, passing_auth, route, body):
    """A non-200 from our own backend must not be billed, and must not be
    handed to the customer as success:true with an empty answer."""
    with patch('core.http_pool.pooled_post',
               side_effect=lambda url, **kw: _resp(
                   401, {'error': 'Authentication required (Bearer token)'})), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        r = app_client.post(route, json=body, headers={'X-API-Key': 'k'})

    payload = r.get_json()
    assert payload.get('success') is not True, (
        'an empty answer was reported as success:true')

    assert passing_auth.called, 'the call should still be logged for audit'
    kwargs = passing_auth.call_args.kwargs
    assert kwargs.get('status_code') not in (None, 200), (
        'the billing row recorded a failed call as 200')
    billable = (kwargs.get('tokens_in', 0) or 0) + (kwargs.get('tokens_out', 0) or 0)
    assert billable == 0, (
        f'customer billed {billable} tokens for a call the backend refused')


@pytest.mark.parametrize('route,body', [
    ('/api/v1/intelligence/chat', {'prompt': 'hello'}),
    ('/api/v1/intelligence/analyze', {'document': 'd', 'question': 'q'}),
])
def test_transport_exception_still_not_billed(app_client, passing_auth,
                                              route, body):
    """The pre-existing except-branch path must also stop billing for a turn
    that produced nothing."""
    with patch('core.http_pool.pooled_post',
               side_effect=RuntimeError('connection refused')), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        r = app_client.post(route, json=body, headers={'X-API-Key': 'k'})

    kwargs = passing_auth.call_args.kwargs
    billable = (kwargs.get('tokens_in', 0) or 0) + (kwargs.get('tokens_out', 0) or 0)
    assert billable == 0
    assert r.get_json().get('success') is not True
