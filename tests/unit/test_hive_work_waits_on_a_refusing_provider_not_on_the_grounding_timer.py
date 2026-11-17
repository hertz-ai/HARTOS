"""The hive worker and the grounding wait follow the same provider rule.

Measured on central 2026-11-16 07:23-07:30Z: 5 of the 10 new lane goals were
auto-paused with cause ledger_grounding ("work was handed to the hive, but the
ledger never reported every task done after 2038-2248s").  Through that
window the distributed worker logged "claiming nothing: waiting (provider
breaker open (openrouter.ai))": it held on the free model's OPEN breaker,
0 calls went to api.anthropic.com, and the daemon's grounding timer
(HEVOLVE_GOAL_VERIFY_TIMEOUT_S, 1800 s) ran on.

Owner decision 2026-11-16 ("Fall back to Haiku"), as one rule
(dispatch.goal_turn_model): with the node's own provider refusing calls, a
claimed task of an earning goal of the node's own runs on the configured
expert where it is offered (lane, Claude day cap, the expert's breaker);
otherwise the worker holds as before.  While a goal's work cannot start that
way the grounding clock does not run, so a provider outage never ends in a
ledger_grounding pause; a real stall still does.

Behavioural: the real DistributedWorkerLoop._tick, _dispatch_would_defer and
_execute_task, the real dispatch.goal_turn_model, configured_expert_offer,
revenue_expert_config and local_chat_dispatch; the real
agent_daemon._settle_dispatched_goal.  Replaced at the boundaries only: the
coordinator (claims, results), the goal row, this node's own in-process /chat
(a tiny Flask app recording each body's model_config), the Claude ledger
(budget_gate.usd_spent_since), the provider breaker's state (a fresh one),
the yield and LLM-slot gates, the guardrails and the world-model record.

    python -m pytest tests/unit/test_hive_work_waits_on_a_refusing_provider_not_on_the_grounding_timer.py -q
"""
import contextlib
import json
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine import agent_daemon, budget_gate, dispatch
from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
from tests.unit.module_swap import swap_modules
from tests.unit.test_a_spent_free_model_hands_goal_turns_to_the_expert import (
    CAP, FREE_HOST, FREE_MODEL, FREE_URL, REPLY, _models)
from tests.unit.test_a_spent_free_model_hands_goal_turns_to_the_expert import (  # noqa: F401
    breaker, spent_free)
from tests.unit.test_revenue_goal_turn_runs_on_the_expert import (  # noqa: F401
    EXPERT_MODEL, expert, no_expert)
from tests.unit.test_speculative_turn_is_judged_by_its_reply import Goal

HIVE_GOAL = 'goal-hive-held-1'


class _Rows:
    """db_session() for the worker's reads: the goal row (whole, or as the
    status and config columns _goal_not_running asks for)."""

    def __init__(self, goal):
        self.goal = goal
        self._columns = False

    @contextlib.contextmanager
    def session(self, *a, **k):
        yield self

    def query(self, *models):
        self._columns = len(models) > 1
        return self

    def filter(self, *a, **kw):
        return self

    def filter_by(self, **kw):
        return self

    def first(self):
        if self.goal is None:
            return None
        if self._columns:
            return (self.goal.status, self.goal.config_json)
        return self.goal

    def count(self):
        return 0


class _Coordinator:
    """The shared coordinator at its boundary: one task to claim, and what
    the worker did with it."""

    def __init__(self, goal_id):
        self.claims = 0
        self.results, self.deferred, self.abandoned = [], [], []
        self.task = types.SimpleNamespace(
            task_id=f'{goal_id}_task_0', parent_task_id=goal_id,
            description='Find parents for the speech therapy at home offer',
            context={'prompt': 'Find parents for the speech therapy offer',
                     'hop': 0, 'goal_type': 'marketing'})

    def claim_next_task(self, agent_id, capabilities=None):
        self.claims += 1
        return self.task

    def submit_result(self, task_id, agent_id, result):
        self.results.append(result)

    def defer_task(self, task_id, agent_id, reason, **kw):
        self.deferred.append(reason)
        return True

    def abandon_task(self, task_id, agent_id):
        self.abandoned.append(task_id)

    def hold_task(self, *a, **k):
        return True


def _own_chat(seen):
    from flask import Flask, jsonify, request
    app = Flask('own_chat_hive_worker')

    @app.post('/chat')
    def _chat():
        seen.append(_models((request.get_json() or {}).get('model_config')))
        return jsonify({'response': REPLY})

    mod = types.ModuleType('hart_intelligence_entry')
    mod.app = app
    return mod


def _worker_tick(goal, ledger, http=None, chat=None):
    """One real worker tick.  Returns (coordinator, model_config of every
    in-process /chat body, the worker).  ``chat``: this node's /chat module
    (default: one that answers REPLY)."""
    seen, coord = [], _Coordinator(HIVE_GOAL)
    w = DistributedWorkerLoop()
    w._node_id = 'node-test'
    http = http or MagicMock(side_effect=AssertionError('no HTTP fallback'))
    chat = chat or _own_chat(seen)
    with contextlib.ExitStack() as stack:
        for p in [
            patch.object(w, '_get_coordinator', return_value=coord),
            patch('integrations.agent_engine.model_registry._own_llm_target',
                  return_value=(FREE_URL, FREE_MODEL)),
            patch('integrations.social.models.db_session', _Rows(goal).session),
            patch.object(budget_gate, 'usd_spent_since',
                         side_effect=lambda since, family: ledger['usd']),
            patch.object(dispatch, 'should_yield_to_user', return_value=False),
            patch.object(dispatch, 'is_user_recently_active', return_value=False),
            patch.object(dispatch, 'local_dispatch_llm_busy', return_value=False),
            patch.object(dispatch, '_internal_auth_headers', return_value={}),
            patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                  side_effect=lambda prompt, *a, **k: (True, '', prompt)),
            patch('security.hive_guardrails.GuardrailEnforcer.after_response',
                  return_value=(True, '')),
            patch('integrations.agent_engine.world_model_bridge.'
                  'get_world_model_bridge'),
            patch('integrations.distributed_agent.worker_loop.pooled_post',
                  http),
            swap_modules({'hart_intelligence_entry': chat,
                          'hartos_backend_adapter': None,
                          'routes.hartos_backend_adapter': None}),
        ]:
            stack.enter_context(p)
        w._tick()
    return coord, seen, w


# ── the worker ─────────────────────────────────────────────────────────────

def test_with_the_free_model_refusing_a_claimed_task_runs_on_the_expert(
        expert, spent_free):
    """FAILS BEFORE THE FIX: the worker claimed nothing ('provider breaker
    open'), and the goal's hive work waited for the reset."""
    coord, seen, _w = _worker_tick(Goal(gid=HIVE_GOAL), {'usd': 0.0})
    assert coord.claims == 1
    assert seen == [[EXPERT_MODEL]], seen
    assert coord.results == [REPLY]


def test_past_the_day_cap_the_worker_holds_and_claims_nothing(expert,
                                                              spent_free):
    coord, seen, w = _worker_tick(Goal(gid=HIVE_GOAL), {'usd': CAP + 0.01})
    assert coord.claims == 0, 'a task was claimed that nothing could run'
    assert seen == []
    assert w._idle_reason == f'waiting (provider breaker open ({FREE_HOST}))'


def test_with_no_expert_the_worker_holds_as_before(no_expert, spent_free):
    coord, seen, w = _worker_tick(Goal(gid=HIVE_GOAL), {'usd': 0.0})
    assert coord.claims == 0
    assert w._idle_reason == f'waiting (provider breaker open ({FREE_HOST}))'


def test_a_claimed_task_the_expert_is_not_offered_for_is_deferred(expert,
                                                                  spent_free):
    """A peer's goal (no row here) never spends this node owner's expert;
    while the free model refuses, its task waits on the DEFERRED lifecycle
    (nothing ran, nothing failed) instead of running into the refusal."""
    coord, seen, _w = _worker_tick(None, {'usd': 0.0})
    assert coord.claims == 1
    assert seen == []
    assert coord.results == [] and coord.abandoned == []
    assert len(coord.deferred) == 1 and FREE_HOST in coord.deferred[0]


def test_with_the_free_model_answering_the_task_runs_on_it(expert):
    """Breaker CLOSED: unchanged.  No model_config, so on native HARTOS the
    in-process route is skipped and the HTTP fallback carries the task, as
    before."""
    http = MagicMock(return_value=types.SimpleNamespace(
        status_code=200, json=lambda: {'response': REPLY}))
    coord, seen, _w = _worker_tick(Goal(gid=HIVE_GOAL), {'usd': 0.0}, http)
    assert coord.claims == 1
    assert seen == []
    assert http.call_count == 1
    assert 'model_config' not in http.call_args.kwargs['json']
    assert coord.results == [REPLY]


# ── the fallback's Claude call is the cached native one ────────────────────
# Owner 2026-11-16 (via cointinue): "all claude api calls should leverage
# prompt caching which was already setup in HARTOS framework".  The fallback
# turn's model calls run inside /chat on autogen, the openai SDK and httpx,
# so the wire hook (core.llm_outbound_logger._send_and_feed) sends each one to
# Anthropic's Messages API with cache breakpoints (core.claude_native), after
# the day-cap gate.  Anthropic is an httpx MockTransport, as in
# test_claude_calls_cache_their_prompt.

ANTHROPIC = 'https://api.anthropic.com/v1'
HAIKU = 'claude-haiku-5-5'
SYSTEM = 'You find parents who want speech therapy at home.'


@pytest.fixture
def anthropic_expert(monkeypatch):
    """Central's expert: Claude Haiku 5.5 at Anthropic, effort high, the
    wire hook installed once on httpx's own send."""
    import httpx
    from core import llm_outbound_logger as hook
    from integrations.agent_engine.model_registry import (
        CONFIGURED_EXPERT_ID, ensure_configured_expert_registered,
        model_registry)
    from tests.unit.test_claude_day_cap_holds_mid_turn import _pristine
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', ANTHROPIC)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', HAIKU)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_API_KEY', 'sk-ant-test-key')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_EFFORT', 'high')
    monkeypatch.delenv('HEVOLVE_EXPERT_LANES', raising=False)
    monkeypatch.setattr(httpx.Client, 'send', _pristine(httpx.Client.send))
    hook._install_sync_patch(httpx)
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    metered = []
    monkeypatch.setattr(budget_gate, 'meter_llm_call',
                        lambda **kw: metered.append(kw.get('model')))
    assert ensure_configured_expert_registered()
    try:
        yield metered
    finally:
        model_registry.unregister(CONFIGURED_EXPERT_ID)


def _claude_chat(handler, answers):
    """This node's /chat for a turn with a model_config override, as far as
    its model calls go: autogen's real OpenAIWrapper over the override with
    this node's own model last (with_local_fallback); the expert's client
    reaches ``handler`` (Anthropic) through httpx."""
    from flask import Flask, jsonify, request
    import autogen
    import httpx
    from core.autogen_config import with_local_fallback
    app = Flask('own_chat_claude_native')

    @app.post('/chat')
    def _chat():
        body = request.get_json() or {}
        config = with_local_fallback(body.get('model_config'))
        config[0] = dict(config[0], http_client=httpx.Client(
            transport=httpx.MockTransport(handler)))
        reply = autogen.OpenAIWrapper(config_list=config, cache_seed=None).create(
            messages=[{'role': 'system', 'content': SYSTEM},
                      {'role': 'user', 'content': body.get('prompt') or 'go'}])
        answers.append(reply.choices[0].message.content)
        return jsonify({'response': answers[-1]})

    mod = types.ModuleType('hart_intelligence_entry')
    mod.app = app
    return mod


def test_a_worker_fallback_call_reaches_claude_natively_with_its_prompt_cached(
        anthropic_expert, spent_free, caplog):
    """FAILS BEFORE THE FIX: the worker claimed nothing, so no call was
    made.  Now the claimed task's turn calls Claude Haiku as the configured
    expert, and the call reaches Anthropic as a Messages API request with
    its cache breakpoints, never at the compatibility layer."""
    import logging
    from tests.unit.test_claude_calls_cache_their_prompt import (EPHEMERAL,
                                                               _anthropic,
                                                               _message)
    handler, reached = _anthropic(_message(
        [{'type': 'text', 'text': 'Found three parents groups.'}]))
    answers = []
    with caplog.at_level(logging.INFO, logger='llm_outbound'):
        coord, _seen, _w = _worker_tick(Goal(gid=HIVE_GOAL), {'usd': 0.0},
                                        chat=_claude_chat(handler, answers))
    assert [r.url.path for r in reached] == ['/v1/messages'], \
        [str(r.url) for r in reached]
    sent = json.loads(reached[0].content)
    assert sent['model'] == HAIKU
    assert sent['cache_control'] == EPHEMERAL
    assert sent['system'] == [{'type': 'text', 'text': SYSTEM,
                               'cache_control': EPHEMERAL}]
    assert sent['output_config'] == {'effort': 'high'}
    assert reached[0].headers['x-api-key'] == 'sk-ant-test-key'
    assert coord.results == ['Found three parents groups.']
    assert anthropic_expert == [HAIKU], 'the call was not metered toward the cap'
    assert any('[claude-native]' in r.getMessage() and 'cache_write=900' in
               r.getMessage() for r in caplog.records)


# ── the grounding wait ─────────────────────────────────────────────────────

class _Progress:
    def get_goal_progress(self, goal_id):
        return {'total_tasks': 1, 'completed': 0}


class _Db:
    def refresh(self, *a, **k):
        pass

    def flush(self):
        pass


def _back(cfg, seconds):
    """Every stamp of the wait ``seconds`` older: time passing."""
    for key in ('awaiting_verification_since', 'last_verification_check'):
        if cfg.get(key):
            cfg[key] = (datetime.fromisoformat(cfg[key])
                        - timedelta(seconds=seconds)).isoformat()


def _await_for(goal, ledger, total_s, every_s=300):
    """Settle ``goal`` as the daemon does after each hive handoff, one check
    every ``every_s`` for ``total_s``, its ledger never done."""
    with patch.object(dispatch, '_get_distributed_coordinator',
                      return_value=_Progress()), \
            patch('integrations.agent_engine.model_registry._own_llm_target',
                  return_value=(FREE_URL, FREE_MODEL)), \
            patch.object(budget_gate, 'usd_spent_since',
                         side_effect=lambda since, family: ledger['usd']):
        agent_daemon._settle_dispatched_goal(_Db(), goal, HIVE_GOAL,
                                             handed_to_hive=True)
        for _ in range(total_s // every_s):
            if goal.status != 'active':
                break
            _back(goal.config_json, every_s)
            agent_daemon._settle_dispatched_goal(_Db(), goal, HIVE_GOAL,
                                                 handed_to_hive=True)


def _hive_goal():
    return Goal(gid=HIVE_GOAL)


def test_a_goal_held_on_a_refusing_provider_is_not_paused_after_1800_s(
        expert, spent_free, monkeypatch):
    """FAILS BEFORE THE FIX: paused 'ledger never reported every task done',
    the five central goals of 07:23-07:30Z."""
    monkeypatch.setenv('HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800')
    goal = _hive_goal()
    _await_for(goal, {'usd': CAP + 0.01}, total_s=2400)
    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert 'awaiting_verification_since' in goal.config_json


@pytest.mark.parametrize('why', ['provider_answering', 'expert_offered'])
def test_a_real_stall_still_pauses_after_1800_s(expert, breaker, monkeypatch,
                                                why):
    """The worker is free to run the goal's task (the free model answering,
    or refusing with the expert offered), and the ledger never finishes: the
    pause stands, as it did before."""
    monkeypatch.setenv('HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800')
    if why == 'expert_offered':
        for _ in range(3):
            breaker.record_failure(FREE_HOST)
    goal = _hive_goal()
    _await_for(goal, {'usd': 0.0}, total_s=2400)
    assert goal.status == 'paused'
    assert 'ledger never reported every task done' in \
        goal.config_json['pause_reason']
