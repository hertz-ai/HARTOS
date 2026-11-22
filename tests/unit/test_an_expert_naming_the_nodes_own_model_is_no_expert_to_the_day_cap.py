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
  One function answers "does the expert trio name the node's own model?":
  core.autogen_config.names_own_llm (the endpoint AND the model, by
  llm_target, spaces around either dropped).  _expert_cap_why asks it
  before it reads the ledger, as the wire's day cap and the registry's
  offer and escalation expert do.

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


# -- both halves count: the endpoint AND the model (review of 9f9981a5b) ----
# Central's setup: the expert is another model behind the gateway the node's
# own model is served from.  The same endpoint with a different model is a
# real expert to every reader of the one rule
# (core.autogen_config.names_own_llm); a rule that compared the endpoint
# alone passed every test above.

def test_the_same_endpoint_with_another_model_is_still_the_expert(cap, monkeypatch):
    _expert(monkeypatch, OWN_URL, EXPERT_MODEL)
    expert = [_entry(OWN_URL, EXPERT_MODEL)]
    assert dispatch._expert_cap_why(model_config=expert) == SPENT
    held = dispatch._model_refusing(expert)
    assert held.startswith('deferred:') and SPENT in held
    assert dispatch.goal_turn_model('marketing', expert) == (expert, held)
    # A judged turn: the expert's host is the node's own host here.
    assert dispatch._expert_cap_why(host='kong.example.org') == SPENT
    # The node's own model on that gateway is not the expert's turn.
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''
    assert set(cap['asked']) == {EXPERT_MODEL}


def test_the_one_rule_needs_the_endpoint_and_the_model_to_match(cap):
    own = (OWN_URL[:-3], OWN_MODEL)
    assert autogen_config.own_llm_target() == own
    assert autogen_config.names_own_llm(_entry(OWN_URL, OWN_MODEL)) is True
    assert autogen_config.names_own_llm(_entry(OWN_URL[:-3], OWN_MODEL)) is True
    assert autogen_config.names_own_llm(_entry(OWN_URL, EXPERT_MODEL)) is False
    assert autogen_config.names_own_llm(_entry(EXPERT_URL, OWN_MODEL)) is False
    assert autogen_config.names_own_llm(_entry(EXPERT_URL, EXPERT_MODEL)) is False
    # A caller that has read the node's own target hands it over.
    assert autogen_config.names_own_llm(_entry(EXPERT_URL, EXPERT_MODEL),
                                        (EXPERT_URL[:-3], EXPERT_MODEL)) is True
    assert autogen_config.names_own_llm(_entry(OWN_URL, OWN_MODEL), ('', '')) is False


def test_the_wire_holds_the_expert_on_the_nodes_own_gateway(cap, monkeypatch):
    """core.llm_outbound_logger._holds_the_expert, the per-call day cap."""
    from types import SimpleNamespace
    import httpx
    from core import llm_outbound_logger as wire
    call = SimpleNamespace(url=httpx.URL(OWN_URL + '/chat/completions'))
    _expert(monkeypatch, OWN_URL, EXPERT_MODEL)
    assert wire._holds_the_expert(call, {'model': EXPERT_MODEL}) is True
    assert wire._holds_the_expert(call, {'model': OWN_MODEL}) is False
    # A trio naming the node's own model is no expert: never refused.
    _expert(monkeypatch, OWN_URL, OWN_MODEL)
    assert wire._holds_the_expert(call, {'model': OWN_MODEL}) is False


def test_the_registry_offers_the_expert_on_the_nodes_own_gateway(cap, monkeypatch):
    """model_registry.ensure_configured_expert_registered, the offer."""
    from integrations.agent_engine import model_registry as registry
    monkeypatch.setattr(registry, '_own_llm_target', lambda: (OWN_URL[:-3], OWN_MODEL))
    try:
        _expert(monkeypatch, OWN_URL, EXPERT_MODEL)
        assert registry.ensure_configured_expert_registered() is True
        assert registry.model_registry.get_model(registry.CONFIGURED_EXPERT_ID) is not None
        _expert(monkeypatch, OWN_URL, OWN_MODEL)
        assert registry.ensure_configured_expert_registered() is False
        assert registry.model_registry.get_model(registry.CONFIGURED_EXPERT_ID) is None
    finally:
        registry.model_registry.unregister(registry.CONFIGURED_EXPERT_ID)


# -- spaces around a configured name are not part of it (review of 537d0a346) --
# The node's own model configured as 'claude-haiku-4-5 ' (a trailing space
# in a settings file) is the model its calls name without the space.  Read
# untrimmed, an expert trio naming that same model looked like another
# model, and the wire, which compares trimmed names, held the node's own
# calls to the expert's day cap.

def test_a_space_after_the_own_models_name_does_not_make_it_another_model(cap, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from core import llm_outbound_logger as wire
    monkeypatch.setattr(autogen_config, 'resolve_llm_backend',
                        lambda: ('api', _entry(OWN_URL + ' ', OWN_MODEL + ' ')))
    _expert(monkeypatch, OWN_URL, OWN_MODEL)
    assert autogen_config.own_llm_target() == (OWN_URL[:-3], OWN_MODEL)
    assert autogen_config.names_own_llm(_entry(OWN_URL, OWN_MODEL)) is True
    call = SimpleNamespace(url=httpx.URL(OWN_URL + '/chat/completions'))
    assert wire._holds_the_expert(call, {'model': OWN_MODEL}) is False, \
        "the node's own call is held to the expert's day cap"
    assert dispatch._expert_cap_why(model_config=[_entry(OWN_URL, OWN_MODEL)]) == ''
    assert cap['asked'] == []


def test_llm_target_drops_the_spaces_around_both_halves():
    assert autogen_config.llm_target(_entry(' https://h.example/v1/ ', ' m ')) == \
        ('https://h.example', 'm')
    assert autogen_config.llm_target(_entry('https://h.example', 'm')) == \
        ('https://h.example', 'm')
    assert autogen_config.llm_target(None) == ('', '')


# -- the escalation expert asks the same rule (review of 537d0a346) ---------
# get_escalation_expert wrote the comparison out a fourth time.

def test_the_escalation_expert_is_never_the_nodes_own_model(cap, monkeypatch):
    from integrations.agent_engine import model_registry as registry
    from integrations.agent_engine.model_registry import ModelBackend, ModelRegistry, ModelTier
    monkeypatch.setattr(registry, '_own_llm_target', lambda: (OWN_URL[:-3], OWN_MODEL))

    def expert(model_id, url, model, accuracy):
        return ModelBackend(
            model_id=model_id, display_name=model_id, tier=ModelTier.EXPERT,
            config_list_entry=_entry(url, model), avg_latency_ms=1000.0,
            accuracy_score=accuracy, cost_per_1k_tokens=0.0, is_local=True)
    book = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, '_offered', staticmethod(lambda m, lane: True))
    monkeypatch.setattr(ModelBackend, 'is_dispatchable', lambda self: True)
    # The node's own model, written with /v1 and a space: a retry, not an expert.
    book._models['own'] = expert('own', OWN_URL, OWN_MODEL + ' ', 0.99)
    assert book.get_escalation_expert() is None
    # Another model on the same gateway is one (both halves count).
    book._models['same-gateway'] = expert('same-gateway', OWN_URL, EXPERT_MODEL, 0.90)
    assert book.get_escalation_expert().model_id == 'same-gateway'
    book._models['elsewhere'] = expert('elsewhere', EXPERT_URL, OWN_MODEL, 0.95)
    assert book.get_escalation_expert().model_id == 'elsewhere'
    # The rule is the shared one: asked once per candidate, with the registry's own target.
    asked = []
    real = autogen_config.names_own_llm
    monkeypatch.setattr(autogen_config, 'names_own_llm',
                        lambda entry, own=None: asked.append(own) or real(entry, own))
    assert book.get_escalation_expert().model_id == 'elsewhere'
    assert asked == [(OWN_URL[:-3], OWN_MODEL)] * 3
