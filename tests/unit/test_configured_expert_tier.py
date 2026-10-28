"""The configured expert tier (owner policy 2026-10-12): Claude on central,
spent only where it drives revenue, never a blocker.

core.autogen_config.resolve_expert_backend reads a second trio
(HEVOLVE_EXPERT_LLM_ENDPOINT_URL / MODEL_NAME / API_KEY); the registry keeps
'configured-expert' equal to it and offers it as the EXPERT tier to the lanes
in HEVOLVE_EXPERT_LANES (default revenue) only.  A user's or the daemon's turn
never sees it, so the free model serves them; a revenue turn gets it first and
falls to the node's own model through the autogen ladder when it fails.

    python -m pytest tests/unit/test_configured_expert_tier.py -q
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import autogen_config as ac  # noqa: E402
from integrations.agent_engine import model_registry as mr  # noqa: E402

OWN = ('http://172.17.0.1:8100/llm', 'qwen3-27b')
EXPERT_ENV = {
    'HEVOLVE_EXPERT_LLM_ENDPOINT_URL': 'http://172.17.0.1:8100/llm/v1',
    'HEVOLVE_EXPERT_LLM_MODEL_NAME': 'claude-sonnet-4-5',
    'HEVOLVE_EXPERT_LLM_API_KEY': 'k',
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in list(EXPERT_ENV) + ['HEVOLVE_EXPERT_LANES']:
        monkeypatch.delenv(v, raising=False)
    ac._reported.discard('expert')
    yield


def _backend(model_id, tier, base_url, model=None, accuracy=0.9):
    return mr.ModelBackend(
        model_id=model_id, display_name=model_id, tier=tier,
        config_list_entry={'model': model or model_id, 'base_url': base_url,
                           'api_key': 'x', 'price': [0, 0]},
        accuracy_score=accuracy)


# ─── resolve_expert_backend ───────────────────────────────────────────────

def test_no_trio_means_no_expert():
    assert ac.resolve_expert_backend() is None


def test_half_a_trio_configures_nothing_and_is_reported_once(monkeypatch, caplog):
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', 'claude-sonnet-4-5')
    assert ac.resolve_expert_backend() is None
    assert ac.resolve_expert_backend() is None
    assert sum('expert tier' in r.getMessage() for r in caplog.records) == 1


def test_the_trio_becomes_a_plain_entry_with_no_thinking_kwargs(monkeypatch):
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    entry = ac.resolve_expert_backend()
    assert entry['model'] == 'claude-sonnet-4-5'
    assert entry['base_url'] == 'http://172.17.0.1:8100/llm/v1'
    assert entry['api_key'] == 'k'
    assert 'extra_body' not in entry          # a hosted frontier model: unknown fields are a 400
    monkeypatch.delenv('HEVOLVE_EXPERT_LLM_API_KEY')
    assert ac.resolve_expert_backend()['api_key'] == 'dummy'


@pytest.mark.parametrize('tier', ['flat', 'regional', 'central'])
def test_configuration_decides_never_the_node_tier(monkeypatch, tier):
    """Topology agnostic (owner, 2026-10-12): the owner wants the expert on
    central only, and gets it because only central's configuration carries
    the trio.  No node-tier branch anywhere: the same trio on any tier
    resolves the same way, and no trio resolves to nothing."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    assert ac.resolve_expert_backend() is None
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    assert ac.resolve_expert_backend()['model'] == 'claude-sonnet-4-5'


def test_expert_lanes_default_to_revenue(monkeypatch):
    assert ac.expert_lanes() == frozenset({'revenue'})
    monkeypatch.setenv('HEVOLVE_EXPERT_LANES', 'revenue, user')
    assert ac.expert_lanes() == frozenset({'revenue', 'user'})


# ─── registration ─────────────────────────────────────────────────────────

@pytest.fixture
def registry(monkeypatch):
    fresh = mr.ModelRegistry()
    monkeypatch.setattr(mr, 'model_registry', fresh)
    with patch.object(mr, '_own_llm_target', return_value=OWN):
        yield fresh


def test_registered_as_expert_priced_like_claude(registry, monkeypatch):
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    for v in ('HEVOLVE_CLAUDE_USD_PER_1K_IN', 'HEVOLVE_SPARK_PER_USD'):
        monkeypatch.delenv(v, raising=False)
    # The claude family price, derived from the one dollar table
    # (core.autogen_config.EXPERT_USD_PER_1K) with the owner's pin: 0.05 * 100
    # = 5 Spark per 1k.  Pinned away from both the list price (2) and the
    # unknown-model default (2), so this proves the derivation, not a
    # coincidence (reviewer of c483a7b32, finding 5).
    monkeypatch.setenv('HEVOLVE_CLAUDE_USD_PER_1K_OUT', '0.05')
    assert mr.ensure_configured_expert_registered() is True
    m = registry.get_model('configured-expert')
    assert m.tier is mr.ModelTier.EXPERT
    assert m.is_dispatchable() and m.is_local
    assert m.cost_per_1k_tokens == 5
    assert m.config_list_entry['model'] == 'claude-sonnet-4-5'
    assert m.config_list_entry['base_url'] == 'http://172.17.0.1:8100/llm/v1'


def test_not_registered_without_the_trio_and_unregistered_when_it_goes(registry, monkeypatch):
    assert mr.ensure_configured_expert_registered() is False
    assert registry.get_model('configured-expert') is None
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    assert mr.ensure_configured_expert_registered() is True
    monkeypatch.delenv('HEVOLVE_EXPERT_LLM_MODEL_NAME')
    assert mr.ensure_configured_expert_registered() is False
    assert registry.get_model('configured-expert') is None


def test_the_nodes_own_model_is_not_an_expert(registry, monkeypatch):
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', 'http://172.17.0.1:8100/llm/v1')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', 'qwen3-27b')
    assert mr.ensure_configured_expert_registered() is False
    assert registry.get_model('configured-expert') is None


def test_reconfiguring_the_trio_replaces_the_entry(registry, monkeypatch):
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    mr.ensure_configured_expert_registered()
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', 'claude-haiku-4-5')
    mr.ensure_configured_expert_registered()
    assert registry.get_model('configured-expert').config_list_entry['model'] == 'claude-haiku-4-5'
    assert len([m for m in registry.list_models() if m.model_id == 'configured-expert']) == 1


# ─── lane gating of the selectors ──────────────────────────────────────────

@pytest.fixture
def lanes(registry, monkeypatch):
    for k, v in EXPERT_ENV.items():
        monkeypatch.setenv(k, v)
    own = _backend('configured-api', mr.ModelTier.FAST,
                   'http://172.17.0.1:8100/llm/v1', 'qwen3-27b', 0.85)
    own.is_local = True                      # as _register_defaults registers it
    registry.register(own)
    mr.ensure_configured_expert_registered()
    # The lane gate is under test here, not the ledger: an empty day.
    from integrations.agent_engine import budget_gate as bg
    monkeypatch.setattr(bg, 'claude_usd_spent_today', lambda: 0.0)
    from core import llm_outbound_logger as lol
    return registry, lol


def test_revenue_lane_gets_the_expert_other_lanes_do_not(lanes):
    registry, lol = lanes
    with lol.lane_context('revenue'):
        assert registry.get_escalation_expert().model_id == 'configured-expert'
        assert registry.get_expert_model().model_id == 'configured-expert'
        fast, expert = registry.speculation_pair()
        assert fast.model_id == 'configured-api' and expert.model_id == 'configured-expert'
    for lane in ('user', 'daemon'):
        with lol.lane_context(lane):
            assert registry.get_escalation_expert() is None
            assert registry.get_expert_model().model_id == 'configured-api'
            assert registry.speculation_pair() == (None, None)


def test_the_caller_may_name_the_lane_as_the_daemon_does(lanes):
    registry, _ = lanes
    assert registry.get_escalation_expert(lane='revenue').model_id == 'configured-expert'
    assert registry.get_escalation_expert(lane='daemon') is None
    assert registry.speculation_pair(lane='revenue')[1].model_id == 'configured-expert'


def test_with_no_lane_in_flight_the_expert_is_not_offered(lanes, monkeypatch):
    registry, lol = lanes
    # No request id, no registered lane: current_lane() says daemon.
    monkeypatch.setattr(lol, '_get_request_id', lambda: '')
    assert registry.get_escalation_expert() is None


def test_owner_can_widen_the_lanes(lanes, monkeypatch):
    registry, lol = lanes
    monkeypatch.setenv('HEVOLVE_EXPERT_LANES', 'revenue,user')
    with lol.lane_context('user'):
        assert registry.get_escalation_expert().model_id == 'configured-expert'
    with lol.lane_context('daemon'):
        assert registry.get_escalation_expert() is None


def test_other_experts_are_not_lane_gated(lanes):
    registry, lol = lanes
    claude_code = _backend('claude-code', mr.ModelTier.EXPERT,
                           'http://127.0.0.1:5000/api/claude/v1', accuracy=0.90)
    registry.register(claude_code)
    with lol.lane_context('daemon'):
        assert registry.get_escalation_expert() is claude_code
    with lol.lane_context('revenue'):
        assert registry.get_escalation_expert().model_id == 'configured-expert'   # 0.95 beats 0.90


# ─── the daemon asks on its goal's lane ───────────────────────────────────

def test_goal_manager_asks_on_the_goals_lane(lanes):
    registry, _ = lanes
    from integrations.agent_engine.goal_manager import GoalManager
    assert GoalManager._escalation_expert('marketing').model_id == 'configured-expert'
    assert GoalManager._escalation_expert('p2p_grocery').model_id == 'configured-expert'
    assert GoalManager._escalation_expert('self_heal') is None
    assert GoalManager._escalation_expert('coding') is None


def test_lane_for_goal_type_is_the_daemon_twin_of_the_chat_rule():
    from integrations.agent_engine.dispatch import lane_for_goal_type
    assert lane_for_goal_type('marketing') == 'revenue'
    assert lane_for_goal_type('p2p_rideshare') == 'revenue'
    assert lane_for_goal_type('robot') == 'daemon'
    assert lane_for_goal_type(None) == 'daemon'


# ─── only the expert selectors may pick it (review of 065cef8bb, C2) ─────────

@pytest.mark.parametrize('lane', ['revenue', 'user', 'daemon'])
def test_fast_local_and_policy_selectors_never_pick_the_configured_expert(lanes, lane):
    registry, lol = lanes
    # The own model slower than the expert and less accurate: the two
    # conditions that let the expert win the fast and local paths before.
    registry.get_model('configured-api').avg_latency_ms = 9000.0
    with lol.lane_context(lane):
        assert registry.get_fast_model().model_id == 'configured-api'
        assert registry.get_fast_model(min_accuracy=0.9) is None
        assert registry.get_local_model().model_id == 'configured-api'
        for policy in ('local_only', 'local_preferred', 'any'):
            assert registry.get_model_by_policy(policy).model_id == 'configured-api'
        if lane == 'revenue':
            fast, expert = registry.speculation_pair()
            assert (fast.model_id, expert.model_id) == ('configured-api', 'configured-expert')


def test_an_unreadable_lane_withholds_the_expert_instead_of_raising(lanes, monkeypatch):
    registry, lol = lanes
    import core.llm_outbound_logger as lol_mod

    def boom():
        raise RuntimeError('no lane')
    monkeypatch.setattr(lol_mod, 'current_lane', boom)
    assert registry.get_escalation_expert() is None
    assert registry.get_expert_model().model_id == 'configured-api'


def test_the_budget_is_read_outside_the_registry_lock(lanes, monkeypatch):
    """The gate reads the spend ledger; holding the registry lock through it
    would stall every other model lookup on a slow DB (review of 065cef8bb,
    M4).  A gate that itself asks the registry proves the lock is free."""
    import threading
    registry, lol = lanes
    from integrations.agent_engine import budget_gate as bg
    seen = {}

    def asks_the_registry(model):
        seen['own'] = registry.get_model('configured-api') is not None   # deadlocks if locked
        return True, 'ok'
    monkeypatch.setattr(bg, 'expert_budget_allows', asks_the_registry)
    result = {}

    def run():
        with lol.lane_context('revenue'):
            result['expert'] = registry.get_escalation_expert()
            result['best'] = registry.get_expert_model()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), 'selector hung: the ledger read ran under the registry lock'
    assert seen == {'own': True}
    assert result['expert'].model_id == 'configured-expert'
    assert result['best'].model_id == 'configured-expert'


# ─── the day cap: not offered past it, never an error ─────────────────────

def test_past_the_day_cap_the_expert_is_not_offered_and_a_model_still_serves(lanes, monkeypatch):
    registry, lol = lanes
    from integrations.agent_engine import budget_gate as bg
    monkeypatch.setattr(bg, 'expert_budget_allows', lambda model: (False, 'spent'))
    with lol.lane_context('revenue'):
        assert registry.get_escalation_expert() is None
        assert registry.get_expert_model().model_id == 'configured-api'   # the node's own model
        assert registry.speculation_pair() == (None, None)
    monkeypatch.setattr(bg, 'expert_budget_allows', lambda model: (True, 'ok'))
    with lol.lane_context('revenue'):
        assert registry.get_escalation_expert().model_id == 'configured-expert'
