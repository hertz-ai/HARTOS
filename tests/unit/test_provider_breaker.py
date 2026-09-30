"""A provider that refuses the account stops goal dispatch until it answers.

Measured on central on 2026-09-14: from 07:26:50Z every hosted chat call
answered 402 insufficient_quota, and the daemon kept dispatching goal turns
that could only fail, each one a failed turn and a wasted tick.  The httpx
hook only watched the local llama-server's ports, so a hosted call on :443
never reached it.  Every chat/completions response now feeds a per-host
breaker (401/402/403 count against the host, 2xx clears it), and
dispatch_goal does not start a turn on a host whose breaker is open.
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import circuit_breaker as breaker_mod
from core import llm_outbound_logger as hook
from integrations.agent_engine import dispatch as dispatch_mod

HOSTED = 'https://api.provider.example/v1/chat/completions'
OTHER = 'https://expert.example/v1'


@pytest.fixture(autouse=True)
def fresh_breaker(monkeypatch):
    fresh = breaker_mod.KeyedCircuitBreaker(threshold=3, cooldown=600.0,
                                            name='llm-provider')
    monkeypatch.setattr(breaker_mod, 'llm_provider_breaker', fresh)
    return fresh


def _chat_request(url=HOSTED):
    return httpx.Request('POST', url, json={'messages': []})


def _send_through_hook(response=None, raises=None, url=HOSTED):
    """One call through the installed sync patch, as the openai SDK makes it."""
    class _Client:
        def send(self, request, **kwargs):
            if raises is not None:
                raise raises
            return response
    hook._install_sync_patch(SimpleNamespace(Client=_Client))
    return _Client().send(_chat_request(url))


# ── The feed ──

def test_a_hosted_refusal_reaches_the_breaker(fresh_breaker):
    for _ in range(3):
        _send_through_hook(httpx.Response(402))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


@pytest.mark.parametrize('status', [401, 403])
def test_every_account_refusal_counts(fresh_breaker, status):
    for _ in range(3):
        _send_through_hook(httpx.Response(status))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


@pytest.mark.parametrize('status', [429, 500, 503])
def test_a_busy_or_broken_provider_does_not_count(fresh_breaker, status):
    for _ in range(5):
        _send_through_hook(httpx.Response(status))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.CLOSED


def test_an_answer_clears_the_count(fresh_breaker):
    _send_through_hook(httpx.Response(402))
    _send_through_hook(httpx.Response(402))
    _send_through_hook(httpx.Response(200))
    _send_through_hook(httpx.Response(402))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.CLOSED


def test_the_response_passes_through_untouched():
    response = httpx.Response(402)
    assert _send_through_hook(response) is response


def test_an_exception_is_reraised_bare_and_not_counted(fresh_breaker):
    boom = httpx.ConnectError('refused')
    with pytest.raises(httpx.ConnectError) as caught:
        _send_through_hook(raises=boom)
    assert caught.value is boom
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.CLOSED


def test_a_request_that_is_not_a_chat_call_is_ignored(fresh_breaker):
    for _ in range(3):
        _send_through_hook(httpx.Response(402),
                           url='https://api.provider.example/v1/embeddings')
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.CLOSED


def test_feed_and_dispatch_name_the_host_the_same_way():
    assert breaker_mod.provider_host(HOSTED) == \
        breaker_mod.provider_host('https://API.Provider.example:443/v1')


# ── dispatch_goal ──

def _open(breaker, host):
    for _ in range(3):
        breaker.record_failure(host)


def _dispatch(model_config=None, goal_id='g-refused'):
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=('https://api.provider.example', 'm')), \
            patch.object(dispatch_mod, 'local_chat_dispatch',
                         return_value=('ok', 'Done.')) as tier1, \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_get_distributed_coordinator',
                         return_value=None), \
            patch.dict('sys.modules', {
                'integrations.agent_engine.budget_gate': SimpleNamespace(
                    pre_dispatch_budget_gate=lambda *a: (True, 'ok')),
                'security.hive_guardrails': SimpleNamespace(
                    GuardrailEnforcer=SimpleNamespace(
                        before_dispatch=lambda p, **k: (True, 'ok', p),
                        after_response=lambda r: (True, 'ok'))),
            }):
        result = dispatch_mod.dispatch_goal('p', 'u1', goal_id, 'marketing',
                                            model_config=model_config)
    return result, tier1


def test_no_turn_starts_on_a_refusing_provider(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    result, tier1 = _dispatch()
    assert result is None
    tier1.assert_not_called()
    assert 'api.provider.example' in \
        dispatch_mod.dispatch_failure_reason('g-refused')


def test_a_goal_waits_rather_than_counting_toward_auto_pause(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=('https://api.provider.example', 'm')), \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_cb_is_open', return_value=False):
        assert dispatch_mod.is_transient_deferral() is True


def test_an_expert_on_another_host_still_runs(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    result, tier1 = _dispatch(model_config=[{'model': 'x', 'base_url': OTHER}])
    assert result == 'Done.'
    tier1.assert_called_once()


def test_after_the_cooldown_a_turn_is_let_through(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    probe = fresh_breaker._get('api.provider.example')
    probe._opened_at = time.monotonic() - probe.cooldown - 1
    result, tier1 = _dispatch()
    assert result == 'Done.'
    tier1.assert_called_once()
