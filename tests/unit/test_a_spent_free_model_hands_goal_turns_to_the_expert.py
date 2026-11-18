"""While the free model's quota is spent, a goal turn runs on the configured
expert; past the Claude day cap it waits, with no strike.

Owner decision 2026-11-16 ("Fall back to Haiku"): when central's free fast
model (OpenRouter) has spent its daily quota and its provider breaker is OPEN,
goal turns run on the node's configured Claude expert (HEVOLVE_EXPERT_LLM_*)
instead of waiting for the 00:00 UTC reset.  The day cap
(HEVOLVE_CLAUDE_BUDGET_USD_DAY) is still read before every expert call; once
it is spent the goals wait, a transient deferral with no strike.

Measured on central 2026-11-16 06:40-07:05Z (build d9afa4f41): 28 HTTP 429s
from openrouter.ai, "[CircuitBreaker:llm-provider:openrouter.ai] OPEN after 3
failures", 0 calls to api.anthropic.com, and the 18 active business goals did
nothing.  Review of 8da02382 (R1, measured with the real tick): the daemon's
speculative branch, which central's goals take, read no breaker before a
turn, so a goal's fast turn ran into the spent quota every tick (52 turns and
52 experts scheduled in 30 minutes, 0 strikes).

One rule, one home (dispatch.goal_turn_model, called by dispatch_goal): the
expert where revenue_expert_config offers it (an earning goal of the node's
own, the revenue lane, the day cap, the expert's own breaker), else no turn
and a wait.  The speculative branch steps aside for a goal while its fast
model's provider refuses (SpeculativeDispatcher.should_speculate).

Behavioural: the real AgentDaemon._tick, the real SpeculativeDispatcher over a
real ModelRegistry, the real dispatch_goal, goal_turn_model,
revenue_expert_config, local_chat_dispatch and is_transient_deferral, and the
real provider breaker (a fresh instance per test).  Replaced at the boundaries
only: the DB rows (test_speculative_turn_is_judged_by_its_reply's tick
harness, and a goal row for dispatch's own reads), the fast /chat transport
(requests.post), this node's own in-process /chat (a tiny Flask app recording
each body's model_config), the expert pool (it holds tasks), the Claude
ledger (budget_gate.usd_spent_since), the platform wallet and the audit log.

    python -m pytest tests/unit/test_a_spent_free_model_hands_goal_turns_to_the_expert.py -q
"""
import contextlib
import time
import types
from unittest.mock import patch

import pytest

from core import circuit_breaker as breaker_mod
from core.agent_tools import user_facing_error
from integrations.agent_engine import agent_daemon, budget_gate, dispatch
from integrations.agent_engine.agent_daemon import AgentDaemon
from integrations.agent_engine.model_registry import (ModelBackend,
                                                      ModelRegistry, ModelTier)
from tests.unit.module_swap import swap_modules
from tests.unit.test_revenue_goal_turn_runs_on_the_expert import (  # noqa: F401
    EXPERT_MODEL, expert, no_expert)
from tests.unit.test_speculative_turn_is_judged_by_its_reply import (
    FAILED, Goal, _Pool, tick)

GOAL_ID = 'goal-spent-free-1'
FREE_URL = 'https://free.test/api/v1'
FREE_HOST = 'free.test'
FREE_MODEL = 'free/model'
CAP = 3.30
REPLY = 'Posted the free first month offer to two parents groups.'
#: What a turn on the free model says once its quota for the day is spent.
QUOTA_SPENT = user_facing_error(RuntimeError(
    'Error code: 429 - Rate limit exceeded: free-models-per-day'))


@pytest.fixture(autouse=True)
def breaker(monkeypatch):
    """A fresh provider breaker, the day cap, and clean daemon state."""
    fresh = breaker_mod.KeyedCircuitBreaker(threshold=3, cooldown=600.0,
                                            name='llm-provider')
    monkeypatch.setattr(breaker_mod, 'llm_provider_breaker', fresh)
    monkeypatch.setenv('HEVOLVE_CLAUDE_BUDGET_USD_DAY', str(CAP))
    monkeypatch.delenv('HEVOLVE_REVENUE_GOAL_TYPES', raising=False)
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)

    def _forget():
        agent_daemon._dispatch_backoff.pop(GOAL_ID, None)
        dispatch._routes_logged.pop(GOAL_ID, None)
        dispatch.dispatch_failure_reason(GOAL_ID)
        budget_gate.invalidate_goal_budget_cache()
        getattr(budget_gate, '_held_reservations', {}).pop(GOAL_ID, None)
    _forget()
    yield fresh
    _forget()


@pytest.fixture
def spent_free(breaker):
    """The free model's day quota is spent: its breaker is OPEN, as the wire
    feed leaves it after three 'free-models-per-day' 429s."""
    for _ in range(3):
        breaker.record_failure(FREE_HOST)
    assert breaker.state(FREE_HOST) == breaker_mod.CircuitState.OPEN
    return breaker


def _speculator(fast_url=FREE_URL, fast_model=FREE_MODEL):
    """The real SpeculativeDispatcher over a real registry holding the free
    model as the fast tier and an expert the speculation selector offers to a
    daemon call (a hive peer's).  What central's registry offers there was
    not read; any second model makes the branch speculate."""
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    reg = ModelRegistry()
    reg.register(ModelBackend(
        'configured-api', 'the free model', ModelTier.FAST,
        {'model': fast_model, 'base_url': fast_url, 'api_key': 'test-only'},
        avg_latency_ms=800.0, accuracy_score=0.6, is_local=True))
    reg.register(ModelBackend(
        'peer-expert', "a peer's expert", ModelTier.EXPERT,
        {'model': 'peer-27b', 'base_url': 'https://peer.test/v1',
         'api_key': 'test-only'},
        accuracy_score=0.9, cost_per_1k_tokens=0.0, is_local=False))
    d = SpeculativeDispatcher(model_registry=reg)
    d._expert_pool.shutdown(wait=False)
    d._expert_pool = _Pool()
    return d


def _models(config):
    return [e.get('model') for e in (config or [])]


class _FastChat:
    """The fast model's /chat at its transport (requests.post, the HTTP
    mode of _dispatch_to_model): records each turn's model and answers with
    what the turn said."""

    def __init__(self, reply):
        self.reply = reply
        self.turns = []

    def __call__(self, url, json=None, headers=None, timeout=None, **_kw):
        assert str(url).endswith('/chat'), url
        self.turns.append(_models((json or {}).get('model_config')))
        return types.SimpleNamespace(status_code=200,
                                     json=lambda: {'response': self.reply})


def _own_chat(seen, reply=REPLY, during=None):
    """This node's own /chat as the in-process transport reaches it
    (dispatch._native_chat posts to hart_intelligence_entry.app): records
    the model_config of each body, runs ``during`` inside the turn."""
    from flask import Flask, jsonify, request
    app = Flask('own_chat_spent_free')

    @app.post('/chat')
    def _chat():
        seen.append(_models((request.get_json() or {}).get('model_config')))
        if during:
            during()
        return jsonify({'response': reply})

    mod = types.ModuleType('hart_intelligence_entry')
    mod.app = app
    return mod


class _Rows:
    """db_session() for the reads dispatch makes in its own sessions: the
    goal row by id (expert_goal_type), and no other active node."""

    def __init__(self, goal):
        self.goal = goal
        self._model = ''

    @contextlib.contextmanager
    def session(self, *a, **k):
        yield self

    def query(self, *models):
        self._model = getattr(models[0], '__name__', '') if models else ''
        return self

    def filter_by(self, **kw):
        return self

    def filter(self, *a, **kw):
        return self

    def first(self):
        return self.goal if self._model == 'AgentGoal' else None

    def count(self):
        return 0


def _tick(goal, d, fast, own, ledger, speculative=True,
          own_target=(FREE_URL, FREE_MODEL)):
    """One real daemon tick for ``goal``: ``own_target`` is this node's own
    model ((url, model), or the real resolver), the Claude ledger reads
    ``ledger['usd']`` (None: the caller models the ledger itself)."""
    own_model = ({'new': own_target} if callable(own_target)
                 else {'return_value': own_target})
    extra = [
        patch('integrations.agent_engine.model_registry._own_llm_target',
              **own_model),
        patch('requests.post', side_effect=fast),
        swap_modules({'hart_intelligence_entry': own,
                      'hartos_backend_adapter': None}),
        patch('integrations.social.models.db_session', _Rows(goal).session),
        patch.object(budget_gate, 'check_platform_affordability',
                     return_value=(True, {})),
        patch('security.immutable_audit_log.get_audit_log'),
        patch.object(AgentDaemon, '_trends_tick'),
        patch.object(dispatch, '_internal_auth_headers', return_value={}),
    ]
    if ledger is not None:
        extra.append(patch.object(budget_gate, 'usd_spent_since',
                                  side_effect=lambda since, family: ledger['usd']))
    tick(goal, d, progress=None, speculative=speculative, extra=extra)


def _strikes():
    return (agent_daemon._dispatch_backoff.get(GOAL_ID) or {}).get('failures', 0)


STYLES = pytest.mark.parametrize('speculative', [True, False],
                                 ids=['speculative', 'dispatch_goal'])


# ── (a) and (d): the free model refusing, the expert offered ────────────────

@STYLES
def test_the_turn_runs_on_the_expert_while_the_free_model_refuses(
        expert, spent_free, speculative):
    """FAILS BEFORE THE FIX on the speculative branch: a fast turn started on
    the refusing provider and an expert was scheduled behind it."""
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    _tick(goal, d, fast, _own_chat(seen), {'usd': 0.0}, speculative)
    assert fast.turns == [], 'a fast turn started on a provider refusing calls'
    assert seen == [[EXPERT_MODEL]], seen
    assert d._expert_pool.jobs == [], 'an expert was scheduled behind a turn'
    assert _strikes() == 0


# ── (b) and (d): past the day cap ───────────────────────────────────────────

@STYLES
def test_past_the_day_cap_the_goal_waits_with_no_strike(expert, spent_free,
                                                        speculative):
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    _tick(goal, d, fast, _own_chat(seen), {'usd': CAP + 0.01}, speculative)
    assert (fast.turns, seen, d._expert_pool.jobs) == ([], [], []), \
        'a turn started though no model would take it'
    assert _strikes() == 0
    assert goal.status == 'active'
    assert FREE_HOST in (dispatch.dispatch_failure_reason(GOAL_ID) or ''), \
        'the wait must name the refusing provider'


# ── (c) and (d): no expert configured ───────────────────────────────────────

@STYLES
def test_with_no_expert_configured_the_goal_waits_with_no_strike(
        no_expert, spent_free, speculative):
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    _tick(goal, d, fast, _own_chat(seen), {'usd': 0.0}, speculative)
    assert (fast.turns, seen, d._expert_pool.jobs) == ([], [], [])
    assert _strikes() == 0
    assert goal.status == 'active'


def test_a_spent_quota_past_the_cap_starts_no_turn_on_any_tick(expert,
                                                               spent_free):
    """R1 in the review's own shape (D3: 52 fast turns and 52 experts in
    30 minutes with the speculative branch): ten ticks, no turn, no expert,
    no strike.  FAILS BEFORE THE FIX: ten fast turns and ten experts."""
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    for _ in range(10):
        _tick(goal, d, fast, _own_chat(seen), {'usd': CAP + 0.01})
    assert len(fast.turns) == 0 and len(d._expert_pool.jobs) == 0, \
        (len(fast.turns), len(d._expert_pool.jobs))
    assert seen == []
    assert _strikes() == 0
    assert goal.status == 'active'


def test_after_the_cooldown_one_fast_turn_probes_the_free_model(expert,
                                                                spent_free):
    """HALF_OPEN lets one turn through, as dispatch_goal and the worker do:
    the wire feed resolves the probe (state() is read, never consumed).  So
    with the quota still spent there is one fast turn per breaker cooldown
    (600 s), not one per tick."""
    probe = spent_free._get(FREE_HOST)
    probe._opened_at = time.monotonic() - probe.cooldown - 1
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(REPLY), []
    _tick(goal, d, fast, _own_chat(seen), {'usd': 0.0})
    assert fast.turns == [[FREE_MODEL]]
    assert seen == []


# ── (f): the free model answering ───────────────────────────────────────────

def test_with_the_free_model_answering_the_goal_still_speculates(expert):
    """Breaker CLOSED: the speculative branch runs as before, and a turn
    that failed on a provider taking calls is still a strike."""
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(FAILED), []
    _tick(goal, d, fast, _own_chat(seen), {'usd': 0.0})
    assert fast.turns == [[FREE_MODEL]]
    assert seen == []
    assert len(d._expert_pool.jobs) == 1
    assert _strikes() == 1


# ── (e): a person's turn ────────────────────────────────────────────────────

def test_a_persons_turn_is_not_gated_by_the_breaker(expert, spent_free):
    """hart_intelligence_entry asks should_speculate with no goal and then
    dispatch_speculative with no goal_id: both unchanged."""
    d, fast = _speculator(), _FastChat(REPLY)
    with patch('requests.post', side_effect=fast), \
            patch('integrations.agent_engine.model_registry._own_llm_target',
                  return_value=(FREE_URL, FREE_MODEL)), \
            patch.object(dispatch, '_internal_auth_headers', return_value={}):
        assert d.should_speculate('person-1', '42', 'What changed today?')
        out = d.dispatch_speculative('What changed today?', 'person-1', '42')
    assert fast.turns == [[FREE_MODEL]]
    assert out['response'] == REPLY
    assert len(d._expert_pool.jobs) == 1


# ── an escalated goal whose expert is refused ───────────────────────────────

@pytest.mark.parametrize('why', ['cap_spent_free_refusing',
                                 'cap_spent_free_answering',
                                 'experts_breaker_open'])
def test_an_escalated_goal_on_a_refused_expert_starts_no_turn(expert, breaker,
                                                             why):
    """A goal whose stuck action was handed to the configured expert (#106d:
    config 'escalation', expert 'configured-expert').  When that expert is
    refused, by the owner's day cap or by its own provider's breaker, no
    turn starts: the goal waits, a transient hold, no strike.  Review of
    242654db3, MF2 (its probe P13): past the cap with the free model
    refusing, the goal ran a doomed turn every tick (each expert call
    refused at the wire and fell to the spent free model), no strike, no
    pause.  FAILS BEFORE THE FIX for the cap: 10 turns in 10 ticks (free
    refusing), or turns and strikes until a pause (free answering)."""
    from integrations.agent_engine.model_registry import CONFIGURED_EXPERT_ID
    from tests.unit.test_revenue_goal_turn_runs_on_the_expert import EXPERT_HOST
    usd = CAP + 0.01 if why.startswith('cap_spent') else 0.0
    if why != 'cap_spent_free_answering':
        for _ in range(3):
            breaker.record_failure(EXPERT_HOST if why == 'experts_breaker_open'
                                   else FREE_HOST)
    goal = Goal(gid=GOAL_ID)
    goal.config_json = dict(goal.config_json, escalation={
        'next': 'expert', 'expert': CONFIGURED_EXPERT_ID, 'at': 't-1',
        'action_id': 3, 'action': 'Post the offer', 'user_prompt': 'grow',
        'prompt_id': '1234', 'flow': 0})
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    for _ in range(10):
        _tick(goal, d, fast, _own_chat(seen, QUOTA_SPENT), {'usd': usd})
    assert (len(seen), len(fast.turns)) == (0, 0), 'a turn started on a refused expert'
    assert _strikes() == 0
    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert goal.config_json['escalation']['next'] == 'expert', \
        'the action stays with the expert for when it is offered again'


# ── what a person reads ─────────────────────────────────────────────────────

def test_a_person_reads_the_wait_without_the_internal_marker(no_expert,
                                                             spent_free,
                                                             tmp_path,
                                                             monkeypatch):
    """The marketplace's distribution runs the real dispatch_goal; the free
    model is refusing and no expert is configured, so the turn is held.
    The person reads why in plain words.  Review of 4642245a7, NF4: the
    held reason's internal 'deferred:' marker reached people ('Distribution
    failed: deferred: provider ...', and the MCP tool's error)."""
    from integrations.agent_engine import app_marketplace as market
    monkeypatch.setattr(market, '_MARKETPLACE_DIR', str(tmp_path))
    monkeypatch.setattr(market, '_LISTINGS_PATH', str(tmp_path / 'listings.json'))
    place = market.AppMarketplace()
    place._save_listings({'L1': {
        'name': 'Tutor', 'description': 'A tutor app', 'owner_id': 'o1',
        'recipe_id': 'r1', 'tagline': 'Learn anything'}})
    with patch('integrations.agent_engine.model_registry._own_llm_target',
               return_value=(FREE_URL, FREE_MODEL)), \
            patch.object(budget_gate, 'pre_dispatch_budget_gate',
                         return_value=(True, 'ok')), \
            patch.object(budget_gate, 'release_goal_reservations',
                         return_value=0), \
            patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                  side_effect=lambda prompt, *a, **k: (True, '', prompt)), \
            patch('security.immutable_audit_log.get_audit_log'), \
            patch.object(dispatch, '_get_distributed_coordinator',
                         return_value=None):
        out = place.distribute_to_channel('L1', 'telegram')
    assert out == {'error': f'Distribution failed: provider {FREE_HOST} is '
                            f'refusing calls (account refused or quota spent; '
                            f'breaker open)'}, out


# ── a turn in flight when the ledger crosses the cap ────────────────────────

@pytest.mark.parametrize('crossed', [True, False],
                         ids=['crossed_the_cap', 'under_the_cap'])
def test_a_turn_that_crossed_the_cap_waits_and_a_failing_expert_still_strikes(
        expert, spent_free, crossed):
    """The expert's turn starts under the cap.  Crossed: the wire refuses its
    later calls (core.llm_outbound_logger._day_cap_refusal), with_local_
    fallback moves them to the free model, whose quota is spent, and the turn
    fails; nothing could have run it, so no strike.  FAILS BEFORE THE FIX: a
    strike, judged by the expert's host.  Under the cap the same failure is
    the expert's own and stays a strike (review of 5c6b899a5).  The wire
    itself is driven in test_a_fallback_expert_turn_stops_at_the_day_cap."""
    ledger = {'usd': CAP - 0.01}

    def _the_turn_spends():
        if crossed:
            ledger['usd'] = CAP + 0.01
    goal = Goal(gid=GOAL_ID)
    d, fast, seen = _speculator(), _FastChat(QUOTA_SPENT), []
    _tick(goal, d, fast, _own_chat(seen, QUOTA_SPENT, _the_turn_spends),
          ledger, speculative=False)
    assert seen == [[EXPERT_MODEL]]
    assert _strikes() == (0 if crossed else 1)
