"""A goal that earns runs the turn it runs here on the configured expert
while the day cap and the expert's provider allow; /chat puts this node's own
model after it.

Owner policy 2026-10-12, as written above dispatch.REVENUE_GOAL_TYPES: "An org
goal that earns ... runs on the REVENUE lane: it may use the expert model and
falls back to the free model when the day fuse trips, so it never stops."
Rulings 2026-10-20/21: Claude only where it drives revenue. Owner 2026-11-03:
"... via the marketing agent sales agent ... since we have claude api
credits there".

Measured on central 2026-11-03 (deployed 9180ba44d, read-only): the expert was
configured, registered and offered to the revenue lane, with $0.00 of the
$3.30 day cap spent, while 21 revenue goals sat paused after 5 failed turns
each on the node's own openrouter/free.  Their turns carried no model_config,
so dispatch_goal ran every one on the node's default model.

Through the REAL transport: dispatch_goal -> local_chat_dispatch ->
_native_chat -> the app's own /chat test client, which JSON-encodes the body.
The app is a tiny Flask app recording the model_config it receives.  The
review of 865afa851 found what a mocked local_chat_dispatch hid: a body
carrying the node's own composed entry (a live http_client) cannot be
encoded, so every revenue dispatch raised; and an OPEN breaker on the
expert's host struck the goal instead of letting it run on the own model.
The other boundaries are test_hive_handoff_is_settled_by_its_run's.  The
expert's day budget (budget_gate.expert_budget_allows) is set per test; the
expert is a throwaway trio on a .test host, removed after every test.

    python -m pytest tests/unit/test_revenue_goal_turn_runs_on_the_expert.py -q
"""
import contextlib
import types
from unittest.mock import patch

import pytest

from core.autogen_config import (EXPERT_API_KEY_VAR, EXPERT_ENDPOINT_VAR,
                                 EXPERT_LANES_VAR, EXPERT_MODEL_VAR)
from integrations.agent_engine import dispatch
from integrations.agent_engine.model_registry import (
    CONFIGURED_EXPERT_ID, ensure_configured_expert_registered, model_registry)
from tests.unit.module_swap import swap_modules
from tests.unit.test_hive_handoff_is_settled_by_its_run import (  # noqa: F401
    Goal, _boundaries, _run, coord)

EXPERT_MODEL = 'claude-test-expert'
EXPERT_HOST = 'expert.test'
REPLY = 'Posted the listing.'
#: The shared boundaries replace the transport and the breaker key; these
#: tests need both real.
_REAL_HERE = {'local_chat_dispatch', '_dispatch_provider_host',
              'local_dispatch_provider_breaker_open'}


@pytest.fixture(autouse=True)
def _default_revenue_set(monkeypatch):
    monkeypatch.delenv('HEVOLVE_REVENUE_GOAL_TYPES', raising=False)


@pytest.fixture
def expert(monkeypatch):
    """A configured expert trio, registered as central registers it at boot."""
    monkeypatch.setenv(EXPERT_ENDPOINT_VAR, f'https://{EXPERT_HOST}/v1')
    monkeypatch.setenv(EXPERT_MODEL_VAR, EXPERT_MODEL)
    monkeypatch.setenv(EXPERT_API_KEY_VAR, 'test-only-key')
    monkeypatch.delenv(EXPERT_LANES_VAR, raising=False)
    assert ensure_configured_expert_registered()
    try:
        yield
    finally:
        model_registry.unregister(CONFIGURED_EXPERT_ID)


@pytest.fixture
def uncapped_expert(monkeypatch):
    """A configured expert outside the capped family: no day cap holds it."""
    monkeypatch.setenv(EXPERT_ENDPOINT_VAR, f'https://{EXPERT_HOST}/v1')
    monkeypatch.setenv(EXPERT_MODEL_VAR, 'gpt-test-expert')
    monkeypatch.setenv(EXPERT_API_KEY_VAR, 'test-only-key')
    monkeypatch.delenv(EXPERT_LANES_VAR, raising=False)
    assert ensure_configured_expert_registered()
    try:
        yield
    finally:
        model_registry.unregister(CONFIGURED_EXPERT_ID)


@pytest.fixture
def no_expert(monkeypatch):
    """A node with no expert trio: a desktop, a regional node."""
    for var in (EXPERT_ENDPOINT_VAR, EXPERT_MODEL_VAR, EXPERT_API_KEY_VAR):
        monkeypatch.delenv(var, raising=False)
    model_registry.unregister(CONFIGURED_EXPERT_ID)
    yield


@pytest.fixture
def refused_expert(expert):
    """The expert's provider is refusing the account: its breaker is OPEN."""
    from core.circuit_breaker import CircuitState, llm_provider_breaker
    for _ in range(5):
        llm_provider_breaker.record_failure(EXPERT_HOST)
    assert llm_provider_breaker.state(EXPERT_HOST) == CircuitState.OPEN
    try:
        yield
    finally:
        llm_provider_breaker.reset(EXPERT_HOST)


def _own_chat_app(seen):
    """This node's /chat as the in-process transport reaches it: a Flask app
    whose test client encodes the body as JSON, recording each model_config."""
    from flask import Flask, jsonify, request
    app = Flask('own_chat')

    @app.post('/chat')
    def _chat():
        seen.append((request.get_json() or {}).get('model_config'))
        return jsonify({'response': REPLY})

    mod = types.ModuleType('hart_intelligence_entry')
    mod.app = app
    return mod


class _Rows:
    """db_session() for dispatch's reads: the goal row by id, and the users
    rows person_for reads (``users``: id -> (user_type, owner_id))."""

    def __init__(self, goal, users):
        self._goal, self._users = goal, users

    @contextlib.contextmanager
    def session(self, *a, **k):
        yield self

    def query(self, model):
        self._model = model.__name__
        return self

    def filter_by(self, id=None, **kw):
        self._id = id
        return self

    def first(self):
        if self._model == 'AgentGoal':
            return self._goal
        kind_owner = self._users.get(self._id)
        if kind_owner is None:
            return None
        return types.SimpleNamespace(id=self._id, user_type=kind_owner[0],
                                     owner_id=kind_owner[1])


def _dispatch(goal, coord, *, model_config=None, budget=(True, 'ok'),
              peers=False, as_type=None, users=None, row=True):
    """Run the real dispatch_goal for ``goal`` through the real in-process
    /chat transport.  Returns (what dispatch_goal returned, the model_config
    of every /chat body received).  ``as_type`` is the goal_type the caller
    names (default: the goal's own); ``row=False``: no goal row here."""
    seen, out = [], {}
    rows = _Rows(goal if row else None, users or {})
    patches = [p for p in _boundaries(goal, coord, None, peers=peers)
               if getattr(p, 'attribute', None) not in _REAL_HERE] + [
        patch('integrations.social.models.db_session', rows.session),
        swap_modules({'hart_intelligence_entry': _own_chat_app(seen),
                      'hartos_backend_adapter': None}),
        patch('integrations.agent_engine.budget_gate.expert_budget_allows',
              return_value=budget),
        patch.object(dispatch, 'is_user_recently_active', return_value=False),
        patch.object(dispatch, '_internal_auth_headers', return_value={}),
        # Tier 2 would reach nothing: a body that could not go in-process
        # fails here instead of reaching a live server.
        patch.object(dispatch, '_local_dispatch_base_url',
                     return_value='http://127.0.0.1:9'),
    ]

    def go():
        out['r'] = dispatch.dispatch_goal(
            'Find riders for the ride-share launch', 'agent-1', goal.id,
            as_type or goal.goal_type, model_config=model_config)
    _run(patches, go)
    return out['r'], seen


def _goal(goal_type):
    g = Goal(gid=f'goal-{goal_type}')
    g.goal_type = goal_type
    return g


def _models(config):
    return [entry.get('model') for entry in (config or [])]


def test_a_revenue_goal_turn_reaches_chat_with_the_expert_alone(expert, coord):
    """FAILS BEFORE THE FIX: the parent sent no model_config (the turn ran on
    the node's own model); 865afa851 sent the expert plus the own entry's live
    http_client, which the transport cannot encode, so it raised."""
    out, seen = _dispatch(_goal('p2p_rideshare'), coord)
    assert out == REPLY
    assert [_models(c) for c in seen] == [[EXPERT_MODEL]], seen


def test_every_kind_of_goal_that_earns_gets_the_expert(expert, coord):
    """Every type in REVENUE_GOAL_TYPES (marketing, SEO, hive growth, the
    McGroce agents, the two sales types registered at boot) and a P2P
    vertical."""
    for goal_type in sorted(dispatch.REVENUE_GOAL_TYPES) + ['p2p_grocery']:
        out, seen = _dispatch(_goal(goal_type), coord)
        assert out == REPLY, goal_type
        assert [_models(c) for c in seen] == [[EXPERT_MODEL]], goal_type


def test_a_goal_that_does_not_earn_never_gets_the_expert(expert, coord):
    """'support' and 'onboarding' have no live producer yet, so a stranger's
    'help' in a group chat can never start a Claude turn (review M3)."""
    for goal_type in ('coding', 'self_heal', 'support', 'onboarding'):
        assert not dispatch.is_revenue_goal_type(goal_type), goal_type
        out, seen = _dispatch(_goal(goal_type), coord)
        assert out == REPLY, goal_type
        assert seen == [None], goal_type


def test_past_the_day_cap_the_turn_runs_on_the_own_model(expert, coord):
    """The cap is the owner's: past it the expert is not offered, and the turn
    runs on the node's own model as it always has. It is never blocked."""
    out, seen = _dispatch(_goal('p2p_rideshare'), coord,
                          budget=(False, 'claude day budget spent: $3.31 of $3.30'))
    assert out == REPLY
    assert seen == [None]


def test_a_refused_expert_account_runs_the_turn_on_the_own_model(
        refused_expert, coord):
    """FAILS BEFORE THE FIX (865afa851): the breaker pre-check keyed on the
    expert's host, the goal got no turn and took a strike (review C2)."""
    out, seen = _dispatch(_goal('p2p_rideshare'), coord)
    assert out == REPLY
    assert seen == [None]


def test_a_caller_naming_its_model_keeps_it(expert, coord):
    """The daemon's escalation turn names its expert itself (#106d)."""
    named = [{'model': 'claude-code', 'base_url': 'http://127.0.0.1:1/v1',
              'api_key': 'x'}]
    out, seen = _dispatch(_goal('marketing'), coord, model_config=named)
    assert out == REPLY
    assert [_models(c) for c in seen] == [['claude-code']]


def test_a_node_without_an_expert_is_unchanged(no_expert, coord):
    """A desktop or a regional node has no expert trio: its revenue goals run
    on its own model exactly as before."""
    out, seen = _dispatch(_goal('p2p_rideshare'), coord)
    assert out == REPLY
    assert seen == [None]


def test_a_revenue_goal_still_goes_to_the_hive_when_it_has_peers(expert, coord):
    """The expert is chosen only for a turn run here, after the hive routing,
    which stays as it was (review M2).  FAILS ON 865afa851: the expert was
    named first, so the goal never went to the hive while the cap allowed."""
    out, seen = _dispatch(_goal('p2p_rideshare'), coord, peers=True)
    assert out is not None
    assert seen == [], 'a goal handed to the hive ran here too'


# ── Only the node's own goals spend the owner's expert (second review of
# 865afa851, M1 and M2).  Measured on central 2026-11-03: all 299 earning
# goals name no owner; any signed-in account can create one that does.

NODE_OWNER = '10202'


def test_a_strangers_earning_goal_runs_on_the_own_model(expert, coord,
                                                        monkeypatch):
    """FAILS BEFORE THE FIX: a goal any account created through POST
    /api/goals (created_by = that account) got the owner's expert."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', NODE_OWNER)
    goal = _goal('marketing')
    goal.owner_id = 'stranger-7'
    out, seen = _dispatch(goal, coord, users={'stranger-7': ('human', None)})
    assert out == REPLY
    assert seen == [None]


def test_the_node_owners_goal_and_their_agents_get_the_expert(
        expert, coord, monkeypatch):
    """The owner's own goal, and a goal of an agent row the owner owns
    (person_for answers the owner for it)."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', NODE_OWNER)
    for owner_id in (NODE_OWNER, 'agent-9'):
        goal = _goal('marketing')
        goal.owner_id = owner_id
        out, seen = _dispatch(goal, coord, users={
            NODE_OWNER: ('human', None), 'agent-9': ('agent', NODE_OWNER)})
        assert out == REPLY, owner_id
        assert [_models(c) for c in seen] == [[EXPERT_MODEL]], owner_id


def test_the_goals_own_type_decides_not_the_callers(expert, coord):
    """FAILS BEFORE THE FIX: dispatch_goal's goal_type defaults to
    'marketing', so a caller naming none (or naming a wrong one) for a
    coding goal got the expert."""
    out, seen = _dispatch(_goal('coding'), coord, as_type='marketing')
    assert out == REPLY
    assert seen == [None]


def test_a_goal_with_no_row_here_never_gets_the_expert(expert, coord):
    """FAILS BEFORE THE FIX: the type argument alone was enough."""
    out, seen = _dispatch(_goal('marketing'), coord, row=False)
    assert out == REPLY
    assert seen == [None]


def test_an_expert_no_day_cap_holds_is_never_named(uncapped_expert, coord):
    """FAILS BEFORE THE FIX: the day cap holds the claude family only
    (budget_gate.expert_budget_allows answers 'not a capped family' for any
    other), so a gpt expert would have run every earning turn uncapped."""
    out, seen = _dispatch(_goal('marketing'), coord)
    assert out == REPLY
    assert seen == [None]
