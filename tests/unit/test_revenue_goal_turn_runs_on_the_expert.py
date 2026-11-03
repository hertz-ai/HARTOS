"""A goal that earns runs its turn on the configured expert while the day cap
allows, with this node's own model as the fallback.

Owner policy 2026-10-12, as written above dispatch.REVENUE_GOAL_TYPES: "An org
goal that earns ... runs on the REVENUE lane: it may use the expert model and
falls back to the free model when the day fuse trips, so it never stops."
Rulings 2026-10-20/21: Claude only where it drives revenue. Owner 2026-11-03:
"... via the marketing agent sales agent ... since we have claude api
credits there".

Measured on central 2026-11-03 (deployed 9180ba44d, read-only):
- The expert was configured (claude-sonnet-5-5), registered, offered to the
  revenue lane, with $0.00 of the $3.30 day cap spent.
- 21 revenue goals were paused after 5 failed turns each, every turn on the
  node's own openrouter/free.
- Their turns are dispatched with no model_config (HEVOLVE_SPECULATIVE_ENABLED
  is unset there), so dispatch_goal ran every one on the node's default model.
  The revenue lane was registered and nothing ever used it.

Behavioural: this calls the real dispatch.dispatch_goal, the real
model_registry.ensure_configured_expert_registered and the real
ModelRegistry lane and budget gate, plus core.autogen_config.with_local_fallback.
The boundaries test_hive_handoff_is_settled_by_its_run replaces are replaced
here too. The expert's day budget (budget_gate.expert_budget_allows) is the
one gate set per test. The expert is a throwaway trio on a .test host, and
its registration is removed after every test.

    python -m pytest tests/unit/test_revenue_goal_turn_runs_on_the_expert.py -q
"""
from unittest.mock import MagicMock, patch

import pytest

from core.autogen_config import (EXPERT_API_KEY_VAR, EXPERT_ENDPOINT_VAR,
                                 EXPERT_LANES_VAR, EXPERT_MODEL_VAR)
from integrations.agent_engine import dispatch
from integrations.agent_engine.model_registry import (
    CONFIGURED_EXPERT_ID, ensure_configured_expert_registered, model_registry)
from tests.unit.test_hive_handoff_is_settled_by_its_run import (  # noqa: F401
    Goal, _boundaries, _run, coord)

EXPERT_MODEL = 'claude-test-expert'


@pytest.fixture
def expert(monkeypatch):
    """A configured expert trio, registered as central registers it at boot."""
    monkeypatch.setenv(EXPERT_ENDPOINT_VAR, 'https://expert.test/v1')
    monkeypatch.setenv(EXPERT_MODEL_VAR, EXPERT_MODEL)
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


def _dispatch(goal, coord, *, model_config=None, budget=(True, 'ok')):
    """Run the real dispatch_goal for ``goal``, here (no other node). Return
    the model_config its /chat turn was given."""
    chat = MagicMock(return_value=('ok', 'Posted the listing.'))
    patches = _boundaries(goal, coord, chat, peers=False) + [
        patch('integrations.agent_engine.budget_gate.expert_budget_allows',
              return_value=budget),
    ]
    _run(patches, lambda: dispatch.dispatch_goal(
        'Find riders for the ride-share launch', 'agent-1', goal.id,
        goal.goal_type, model_config=model_config))
    assert chat.call_count == 1, 'the turn did not run'
    return chat.call_args.kwargs.get('model_config')


def _goal(goal_type):
    g = Goal(gid=f'goal-{goal_type}')
    g.goal_type = goal_type
    return g


def test_a_revenue_goal_turn_runs_on_the_expert_then_the_own_model(expert, coord):
    """FAILS BEFORE THE FIX: the turn got no model_config, so it ran on the
    node's own model and the expert was never used (central's 21 paused
    goals, $0.00 of the cap spent)."""
    cfg = _dispatch(_goal('p2p_rideshare'), coord)
    assert cfg, 'a revenue goal ran on the default model, not the expert'
    assert cfg[0]['model'] == EXPERT_MODEL
    assert len(cfg) >= 2 and cfg[-1]['model'] != EXPERT_MODEL, (
        'no fallback to the node\'s own model after the expert', cfg)


def test_every_revenue_type_gets_the_expert(expert, coord):
    """The same for the other kinds of goal that earn, marketing among
    them. FAILS BEFORE THE FIX."""
    for goal_type in ('marketing', 'seo', 'revenue', 'hive_growth', 'p2p_grocery'):
        cfg = _dispatch(_goal(goal_type), coord)
        assert cfg and cfg[0]['model'] == EXPERT_MODEL, goal_type


def test_sales_customer_care_and_onboarding_turns_get_the_expert(expert, coord):
    """The owner named the sales and customer-care agents for the Claude
    credits (2026-11-03), and ruled sales and onboarding revenue goals
    (2026-10-21): the outreach reply (outreach_crm_tools), the support
    reply (hive_signal_bridge) and the buyer welcome (app_marketplace).
    FAILS BEFORE THE FIX: none of the three types was in the revenue set."""
    for goal_type in ('outreach', 'support', 'onboarding'):
        assert dispatch.is_revenue_goal_type(goal_type), goal_type
        cfg = _dispatch(_goal(goal_type), coord)
        assert cfg and cfg[0]['model'] == EXPERT_MODEL, goal_type


def test_past_the_day_cap_a_revenue_goal_runs_on_the_own_model(expert, coord):
    """The cap is the owner's: past it the expert is not offered, and the
    turn runs on the node's own model as it always has. It is never
    blocked."""
    cfg = _dispatch(_goal('p2p_rideshare'), coord,
                    budget=(False, 'claude day budget spent: $3.31 of $3.30'))
    assert cfg is None


def test_a_goal_that_does_not_earn_never_gets_the_expert(expert, coord):
    for goal_type in ('coding', 'self_heal', 'outreach_unknown_type'):
        assert _dispatch(_goal(goal_type), coord) is None, goal_type


def test_a_caller_naming_its_model_keeps_it(expert, coord):
    """The daemon's escalation turn names its expert itself (#106d); its
    choice stands."""
    named = [{'model': 'claude-code', 'base_url': 'http://127.0.0.1:1/v1',
              'api_key': 'x'}]
    assert _dispatch(_goal('marketing'), coord, model_config=named) == named


def test_a_node_without_an_expert_is_unchanged(no_expert, coord):
    """A desktop or a regional node has no expert trio: its revenue goals
    run on its own model exactly as before."""
    assert _dispatch(_goal('p2p_rideshare'), coord) is None
