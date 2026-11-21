"""An expert trio that names the node's own model is no expert to the day cap.

WHAT WAS WRONG (audit row H14, copy)
  Three places ask "is this the configured expert?": the model registry's
  offer (ensure_configured_expert_registered), the wire's per-call day cap
  (core.llm_outbound_logger._holds_the_expert) and the goal-turn hold
  (integrations.agent_engine.dispatch._expert_cap_why).  The first two say an
  expert trio naming the node's own endpoint and model is no expert: it is
  never offered and its calls are never refused.  _expert_cap_why had no such
  test.  With such a trio and the Claude day cap spent, a goal turn handed
  the node's own model as its model_config was answered 'deferred: the
  configured expert is withheld' on every tick, a hold nothing would ever
  lift before the day ended, although the wire would have let every one of
  its calls through.

THE RULE
  _expert_cap_why applies the shared rule (core.autogen_config.llm_target
  against own_llm_target) before it reads the ledger.

    python -m pytest tests/unit/test_an_expert_naming_the_nodes_own_model_is_no_expert_to_the_day_cap.py -q
"""
import pytest

from core import autogen_config
from integrations.agent_engine import budget_gate, dispatch

OWN_URL = 'https://kong.example.org/claude/v1'
OWN_MODEL = 'claude-haiku-4-5'
EXPERT_URL = 'https://api.expert.example.com/v1'
EXPERT_MODEL = 'claude-sonnet-4-5'
SPENT = 'claude day budget spent: $6.10 of $6.00'


def _entry(url, model):
    return {'model': model, 'api_key': 'k', 'base_url': url}


@pytest.fixture
def cap(monkeypatch):
    """The ledger's answer, and what it was asked."""
    state = {'allowed': False, 'asked': []}

    def allows(model):
        state['asked'].append(model)
        return state['allowed'], ('' if state['allowed'] else SPENT)
    monkeypatch.setattr(budget_gate, 'expert_budget_allows', allows)
    monkeypatch.setattr(autogen_config, 'resolve_llm_backend',
                        lambda: ('api', _entry(OWN_URL, OWN_MODEL)))
    monkeypatch.setattr(dispatch, 'local_dispatch_provider_breaker_open',
                        lambda model_config=None: '')
    return state


def _expert(monkeypatch, url, model):
    monkeypatch.setenv(autogen_config.EXPERT_ENDPOINT_VAR, url)
    monkeypatch.setenv(autogen_config.EXPERT_MODEL_VAR, model)
    monkeypatch.setenv(autogen_config.EXPERT_API_KEY_VAR, 'k')


# -- the defect --

def test_a_turn_on_the_nodes_own_model_is_not_held_by_the_cap(cap, monkeypatch):
    _expert(monkeypatch, OWN_URL, OWN_MODEL)
    own = [_entry(OWN_URL, OWN_MODEL)]
    assert dispatch._expert_cap_why(model_config=own) == ''
    assert dispatch._model_refusing(own) == ''
    assert dispatch.goal_turn_model('marketing', own) == (own, '')
    assert cap['asked'] == [], 'the ledger is read only for the expert'


def test_the_own_model_written_without_v1_is_the_same_model(cap, monkeypatch):
    """llm_target's rule: the same endpoint is written both ways."""
    _expert(monkeypatch, OWN_URL[:-3], OWN_MODEL)
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''


def test_a_judged_turn_on_the_own_models_host_is_not_the_experts(cap, monkeypatch):
    _expert(monkeypatch, OWN_URL, OWN_MODEL)
    assert dispatch._expert_cap_why(host='kong.example.org') == ''


# -- carried over: these pass before the fix and after it --

def test_a_real_expert_past_the_cap_is_withheld(cap, monkeypatch):
    _expert(monkeypatch, EXPERT_URL, EXPERT_MODEL)
    expert = [_entry(EXPERT_URL, EXPERT_MODEL)]
    assert dispatch._expert_cap_why(model_config=expert) == SPENT
    held = dispatch._model_refusing(expert)
    assert held.startswith('deferred:') and SPENT in held
    assert dispatch.goal_turn_model('marketing', expert) == (expert, held)
    assert cap['asked'] and set(cap['asked']) == {EXPERT_MODEL}


def test_the_same_model_name_at_another_endpoint_is_a_real_expert(cap, monkeypatch):
    _expert(monkeypatch, EXPERT_URL, OWN_MODEL)
    expert = [_entry(EXPERT_URL, OWN_MODEL)]
    assert dispatch._expert_cap_why(model_config=expert) == SPENT
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''


def test_a_real_expert_under_the_cap_runs(cap, monkeypatch):
    _expert(monkeypatch, EXPERT_URL, EXPERT_MODEL)
    cap['allowed'] = True
    expert = [_entry(EXPERT_URL, EXPERT_MODEL)]
    assert dispatch._expert_cap_why(model_config=expert) == ''
    assert dispatch._model_refusing(expert) == ''


def test_another_model_is_never_asked_about_the_cap(cap, monkeypatch):
    _expert(monkeypatch, EXPERT_URL, EXPERT_MODEL)
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''
    assert dispatch._expert_cap_why(host='kong.example.org') == ''
    assert cap['asked'] == []


def test_a_judged_turn_on_the_experts_host_reads_the_cap(cap, monkeypatch):
    _expert(monkeypatch, EXPERT_URL, EXPERT_MODEL)
    assert dispatch._expert_cap_why(host='api.expert.example.com') == SPENT


def test_no_expert_configured_withholds_nothing(cap, monkeypatch):
    for var in (autogen_config.EXPERT_ENDPOINT_VAR, autogen_config.EXPERT_MODEL_VAR):
        monkeypatch.delenv(var, raising=False)
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''
    assert cap['asked'] == []
