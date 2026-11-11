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


def _quota_429(remaining='0', reset_in_s=6 * 3600.0, ms=True):
    """openrouter's answer once its free calls for the day are used, as
    measured on central 2026-11-11 (the reset is the next 00:00 UTC, in ms)."""
    reset = time.time() + reset_in_s
    return httpx.Response(429, json={'error': {
        'message': 'Rate limit exceeded: free-models-per-day. Add 10 credits '
                   'to unlock 1000 free model requests per day',
        'code': 429,
        'metadata': {'headers': {
            'X-RateLimit-Limit': '50', 'X-RateLimit-Remaining': remaining,
            'X-RateLimit-Reset': str(int(reset * 1000 if ms else reset))},
            'limit_source': 'openrouter_free_tier_daily'}}})


def test_a_quota_spent_for_the_day_counts_like_a_refusal(fresh_breaker):
    """Central 2026-11-11: past the Claude cap, goal turns ran on the free
    model, whose daily quota was gone; every turn failed and 19 of 24 goals
    paused themselves.  A quota spent until a reset hours away now holds
    goal dispatch like a 402."""
    for _ in range(3):
        _send_through_hook(_quota_429())
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


def test_a_quota_reset_in_seconds_since_the_epoch_counts_too(fresh_breaker):
    for _ in range(3):
        _send_through_hook(_quota_429(ms=False))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


@pytest.mark.parametrize('remaining,reset_in_s', [
    ('3', 6 * 3600.0),     # calls left: an ordinary rate limit
    ('0', -60.0),          # spent, but the reset has already passed
])
def test_a_429_with_calls_left_or_a_past_reset_does_not_count(fresh_breaker,
                                                              remaining, reset_in_s):
    for _ in range(5):
        _send_through_hook(_quota_429(remaining=remaining, reset_in_s=reset_in_s))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.CLOSED


@pytest.mark.parametrize('reset_in_s', [60.0, 3600.0, 21599.0])
def test_a_quota_spent_until_a_near_reset_counts_too(fresh_breaker, reset_in_s):
    """Review of 5c6b899a5: a quota counted only when its reset was more than
    600 s away let the breaker reopen in the last minutes before every reset,
    where each goal turn failed and took a strike (4 measured, and a goal
    paused as early as without the fix).  Any reset still ahead counts."""
    for _ in range(3):
        _send_through_hook(_quota_429(reset_in_s=reset_in_s))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


def test_the_quota_headers_are_read_whatever_their_case(fresh_breaker):
    reset_ms = str(int((time.time() + 6 * 3600.0) * 1000))
    body = {'error': {'code': 429, 'message': 'Rate limit exceeded',
                      'metadata': {'headers': {'x-ratelimit-remaining': '0',
                                               'x-ratelimit-reset': reset_ms}}}}
    for _ in range(3):
        _send_through_hook(httpx.Response(429, json=body))
    assert fresh_breaker.state('api.provider.example') is \
        breaker_mod.CircuitState.OPEN


# The other three of the four call sites that hand the response to the feed:
# the local llama-server's path (sync), and the async hosted and local paths.
LOCAL = 'http://127.0.0.1:8082/v1/chat/completions'


@pytest.fixture
def local_target(monkeypatch):
    """The hook's local-server path without its side work: port 8082 is the
    target, the n_ctx trim, the outbound record and the slot scheduler are
    out of the way (they do not decide what reaches the breaker)."""
    monkeypatch.setattr(hook, '_target_ports', lambda: {8082})
    monkeypatch.setattr(hook, '_apply_trim_to_request', lambda m, r, b: (b, None))
    monkeypatch.setattr(hook, 'log_outbound', lambda *a, **k: None)
    monkeypatch.setattr(hook, '_select_send_client', lambda self, request: self)
    import core.llama_scheduler as sched
    monkeypatch.setattr(sched, 'get_scheduler',
                        lambda: (_ for _ in ()).throw(RuntimeError('no scheduler')))


def test_the_local_server_path_feeds_a_spent_quota_too(fresh_breaker, local_target):
    for _ in range(3):
        _send_through_hook(_quota_429(), url=LOCAL)
    assert fresh_breaker.state(breaker_mod.provider_host(LOCAL)) is \
        breaker_mod.CircuitState.OPEN


def _send_through_async_hook(response, url):
    import asyncio

    class _AsyncClient:
        async def send(self, request, **kwargs):
            return response
    hook._install_async_patch(SimpleNamespace(AsyncClient=_AsyncClient))
    return asyncio.run(_AsyncClient().send(_chat_request(url)))


@pytest.mark.parametrize('url', [HOSTED, LOCAL], ids=['hosted', 'local'])
def test_the_async_paths_feed_a_spent_quota_too(fresh_breaker, local_target, url):
    for _ in range(3):
        _send_through_async_hook(_quota_429(), url)
    assert fresh_breaker.state(breaker_mod.provider_host(url)) is \
        breaker_mod.CircuitState.OPEN


def test_a_spent_quota_makes_the_goal_wait_not_fail(fresh_breaker):
    """The end the owner sees: the goal waits for the reset instead of
    counting a strike toward its auto-pause."""
    for _ in range(3):
        _send_through_hook(_quota_429())
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=('https://api.provider.example', 'm')), \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_cb_is_open', return_value=False):
        assert dispatch_mod.is_transient_deferral() is True


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


def _dispatch(model_config=None, goal_id='g-refused', reply='Done.'):
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=('https://api.provider.example', 'm')), \
            patch.object(dispatch_mod, 'local_chat_dispatch',
                         return_value=('ok', reply)) as tier1, \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_get_distributed_coordinator',
                         return_value=None), \
            patch.dict('sys.modules', {
                'integrations.agent_engine.budget_gate': SimpleNamespace(
                    pre_dispatch_budget_gate=lambda *a, **k: (True, 'ok'),
                    release_goal_reservations=lambda *a, **k: 0,
                    reservation_holder=lambda kind, ident: f'{kind}:{ident}'),
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


def _deferral(goal_id):
    """is_transient_deferral(goal_id) asked as the daemon asks it, from the
    thread that dispatched, with no person active and the Tier-2 breaker
    closed."""
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=('https://api.provider.example', 'm')), \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_cb_is_open', return_value=False):
        return dispatch_mod.is_transient_deferral(goal_id)


def test_a_failed_expert_turn_is_a_failure_while_the_own_provider_waits(fresh_breaker):
    """Review of 5c6b899a5: is_transient_deferral judged every goal by the
    node's own provider, so while the free model's quota was spent a failing
    turn on the configured expert took no strike and no backoff (20 turns in
    570 s against 4 strikes before).  A goal waits only for the provider its
    own turn used."""
    from core.agent_tools import is_user_facing_error, user_facing_error
    failed = user_facing_error(RuntimeError('Error code: 529 - Overloaded'))
    assert is_user_facing_error(failed)
    _open(fresh_breaker, 'api.provider.example')
    result, tier1 = _dispatch(model_config=[{'model': 'x', 'base_url': OTHER}],
                              goal_id='g-expert', reply=failed)
    tier1.assert_called_once()
    assert result is None
    assert _deferral('g-expert') is False


def test_a_goal_held_on_its_own_waiting_provider_is_a_deferral(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    result, tier1 = _dispatch(goal_id='g-own')
    tier1.assert_not_called()
    assert result is None
    assert _deferral('g-own') is True


def test_after_the_cooldown_a_turn_is_let_through(fresh_breaker):
    _open(fresh_breaker, 'api.provider.example')
    probe = fresh_breaker._get('api.provider.example')
    probe._opened_at = time.monotonic() - probe.cooldown - 1
    result, tier1 = _dispatch()
    assert result == 'Done.'
    tier1.assert_called_once()
