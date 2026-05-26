"""Tests for draft-first speculative dispatch (Qwen3.5-0.8B first-responder).

Covers:
  - ModelRegistry.get_draft_model() selects the DRAFT tier only
  - get_fast_model() and get_expert_model() now exclude DRAFT tier
  - dispatch_draft_first parses valid JSON envelope from draft output
  - dispatch_draft_first handles prose-wrapped / fenced / malformed envelopes
  - delegate routing: 'none' → no expert; 'local' → fast expert; 'hive' → expert fallback to fast
  - Recursive loop prevention: inner _dispatch_to_model sends speculative=False, draft_first=False
  - WorldModelBridge.record_interaction is called with the draft model_id tag
  - Graceful fallback when no draft model is registered
  - Circuit-breaker / constitutional filter short-circuit paths
"""
import os
import sys
import json
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def fresh_registry():
    """Fresh isolated ModelRegistry per test — no singleton contamination."""
    from integrations.agent_engine.model_registry import ModelRegistry, ModelBackend, ModelTier
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-0.8b-draft',
        display_name='Qwen3.5 0.8B (Draft)',
        tier=ModelTier.DRAFT,
        config_list_entry={'model': 'Qwen3.5-0.8B-Instruct', 'api_key': 'dummy',
                           'base_url': 'http://localhost:8080/v1', 'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.45, cost_per_1k_tokens=0.0,
        is_local=True,
    ))
    reg.register(ModelBackend(
        model_id='qwen3.5-4b-local',
        display_name='Qwen3.5 4B (Fast)',
        tier=ModelTier.FAST,
        config_list_entry={'model': 'Qwen3.5-4B', 'api_key': 'dummy',
                           'base_url': 'http://localhost:8080/v1', 'price': [0, 0]},
        avg_latency_ms=700.0, accuracy_score=0.60, cost_per_1k_tokens=0.0,
        is_local=True,
    ))
    reg.register(ModelBackend(
        model_id='gpt-4-expert',
        display_name='GPT-4 (Expert)',
        tier=ModelTier.EXPERT,
        config_list_entry={'model': 'gpt-4', 'api_key': 'dummy',
                           'base_url': 'https://api.openai.com/v1', 'price': [0.03, 0.06]},
        avg_latency_ms=3000.0, accuracy_score=0.90, cost_per_1k_tokens=60.0,
    ))
    return reg


@pytest.fixture
def dispatcher(fresh_registry):
    """SpeculativeDispatcher bound to the fresh registry.

    Tests stub _dispatch_to_model directly, so the production health
    probe against the draft server (port 8081 TCP connect) must be
    disabled — otherwise the gate short-circuits before the mock runs
    and dispatch_draft_first returns a gate-error envelope missing
    is_correction / is_casual / etc.
    """
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
    d = SpeculativeDispatcher(model_registry=fresh_registry)
    d._health_probe_enabled = False
    return d


# ═══════════════════════════════════════════════════════════════════════════
# Registry tier selection
# ═══════════════════════════════════════════════════════════════════════════


class TestRegistryTierSelection:

    def test_get_draft_model_returns_draft_tier(self, fresh_registry):
        draft = fresh_registry.get_draft_model()
        assert draft is not None
        assert draft.model_id == 'qwen3.5-0.8b-draft'

    def test_get_draft_model_none_when_absent(self):
        from integrations.agent_engine.model_registry import ModelRegistry, ModelBackend, ModelTier
        reg = ModelRegistry()
        reg.register(ModelBackend(
            model_id='only-fast',
            display_name='Only Fast',
            tier=ModelTier.FAST,
            config_list_entry={'model': 'x', 'api_key': 'y'},
            avg_latency_ms=500.0, accuracy_score=0.6,
        ))
        assert reg.get_draft_model() is None

    def test_get_fast_model_excludes_draft(self, fresh_registry):
        """DRAFT tier must NEVER be returned by get_fast_model — even if it
        has the lowest latency, because the draft can't produce final
        answers for complex tasks."""
        fast = fresh_registry.get_fast_model()
        assert fast is not None
        assert fast.model_id == 'qwen3.5-4b-local'
        assert fast.tier.value != 'draft'

    def test_get_expert_model_excludes_draft(self, fresh_registry):
        expert = fresh_registry.get_expert_model()
        assert expert is not None
        assert expert.tier.value != 'draft'
        assert expert.model_id == 'gpt-4-expert'

    def test_draft_has_lower_latency_than_fast(self, fresh_registry):
        draft = fresh_registry.get_draft_model()
        fast = fresh_registry.get_fast_model()
        assert draft.avg_latency_ms < fast.avg_latency_ms


# ═══════════════════════════════════════════════════════════════════════════
# JSON envelope parser
# ═══════════════════════════════════════════════════════════════════════════


class TestDraftEnvelopeParser:

    def test_clean_json(self, dispatcher):
        raw = '{"reply": "hi there", "delegate": "none", "confidence": 0.9, "reason": "greeting"}'
        parsed = dispatcher._parse_draft_envelope(raw)
        assert parsed['reply'] == 'hi there'
        assert parsed['delegate'] == 'none'
        assert parsed['confidence'] == 0.9

    def test_markdown_fenced(self, dispatcher):
        raw = '```json\n{"reply": "standby", "delegate": "local", "confidence": 0.5}\n```'
        parsed = dispatcher._parse_draft_envelope(raw)
        assert parsed['reply'] == 'standby'
        assert parsed['delegate'] == 'local'

    def test_prose_wrapped_json(self, dispatcher):
        raw = 'Here is my answer: {"reply": "ok", "delegate": "hive", "confidence": 0.3} hope it helps!'
        parsed = dispatcher._parse_draft_envelope(raw)
        assert parsed['delegate'] == 'hive'

    def test_trailing_comma_tolerated(self, dispatcher):
        raw = '{"reply": "x", "delegate": "none", "confidence": 1.0,}'
        parsed = dispatcher._parse_draft_envelope(raw)
        assert parsed.get('reply') == 'x'

    def test_completely_malformed_returns_empty(self, dispatcher):
        assert dispatcher._parse_draft_envelope('not json at all') == {}

    def test_empty_input_returns_empty(self, dispatcher):
        assert dispatcher._parse_draft_envelope('') == {}
        assert dispatcher._parse_draft_envelope(None) == {}


# ═══════════════════════════════════════════════════════════════════════════
# dispatch_draft_first — delegate routing
# ═══════════════════════════════════════════════════════════════════════════


def _mock_guardrails(monkeypatch):
    """Bypass the real guardrail network so tests stay hermetic."""
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
    monkeypatch.setattr(
        hg.ConstitutionalFilter, 'check_prompt',
        staticmethod(lambda p: (True, ''))
    )


class TestDispatchDraftFirstDelegation:

    def test_delegate_none_returns_draft_reply_no_expert(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "Hello!", "delegate": "none", "confidence": 0.95}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw) as mock_dispatch, \
             patch.object(dispatcher, '_record_interaction_safely') as mock_record, \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'hi', user_id='u1', prompt_id='p1')

        assert result['response'] == 'Hello!'
        assert result['delegate'] == 'none'
        assert result['draft_model'] == 'qwen3.5-0.8b-draft'
        assert result['expert_pending'] is False
        mock_dispatch.assert_called_once()
        mock_submit.assert_not_called()
        # Draft interaction was recorded for continual learning
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args.kwargs
        assert call_kwargs['model_id'] == 'qwen3.5-0.8b-draft'
        assert call_kwargs['response'] == 'Hello!'

    def test_delegate_local_fires_background_expert(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "Working on it...", "delegate": "local", "confidence": 0.4}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'write a function to reverse a list', user_id='u1', prompt_id='p1')

        assert result['delegate'] == 'local'
        assert result['response'] == 'Working on it...'
        assert result['expert_pending'] is True
        # Expert task was scheduled
        mock_submit.assert_called_once()
        # The scheduled model is the FAST tier (4B), not the draft or expert
        submit_args = mock_submit.call_args.args
        scheduled_model = submit_args[4]  # expert_model is the 5th positional arg
        assert scheduled_model.tier.value == 'fast'

    def test_delegate_hive_prefers_expert_tier(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "Let me check the hive...", "delegate": "hive", "confidence": 0.2}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'prove the Riemann hypothesis', user_id='u1', prompt_id='p1')

        assert result['delegate'] == 'hive'
        assert result['expert_pending'] is True
        scheduled_model = mock_submit.call_args.args[4]
        # Hive delegate picks the EXPERT tier (highest accuracy)
        assert scheduled_model.tier.value == 'expert'

    def test_delegate_hive_falls_back_to_fast_when_no_expert(self, monkeypatch):
        """If only a draft + fast are registered and delegate='hive',
        the dispatcher falls back to the fast tier instead of returning
        no expert at all."""
        from integrations.agent_engine.model_registry import ModelRegistry, ModelBackend, ModelTier
        from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
        reg = ModelRegistry()
        reg.register(ModelBackend(
            model_id='draft-only', display_name='D', tier=ModelTier.DRAFT,
            config_list_entry={'model': 'd', 'api_key': 'x'},
            avg_latency_ms=200.0, accuracy_score=0.4, is_local=True,
        ))
        reg.register(ModelBackend(
            model_id='fast-only', display_name='F', tier=ModelTier.FAST,
            config_list_entry={'model': 'f', 'api_key': 'x'},
            avg_latency_ms=700.0, accuracy_score=0.6, is_local=True,
        ))
        d = SpeculativeDispatcher(model_registry=reg)
        d._health_probe_enabled = False
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "trying", "delegate": "hive", "confidence": 0.2}'
        with patch.object(d, '_dispatch_to_model', return_value=draft_raw), \
             patch.object(d, '_record_interaction_safely'), \
             patch.object(d, '_check_and_reserve_budget', return_value=True), \
             patch.object(d._expert_pool, 'submit') as mock_submit:
            result = d.dispatch_draft_first('hard question', user_id='u1', prompt_id='p1')

        assert result['delegate'] == 'hive'
        assert result['expert_pending'] is True
        assert mock_submit.call_args.args[4].model_id == 'fast-only'

    def test_malformed_json_defaults_to_local_delegate(
            self, dispatcher, monkeypatch):
        """Tiny models sometimes emit prose instead of JSON — we should
        NOT silently treat that as 'none' (which would hide the failure).
        Default to 'local' so a bigger model confirms the answer."""
        _mock_guardrails(monkeypatch)
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value='I think the answer is 42.'), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'whatever', user_id='u1', prompt_id='p1')

        assert result['delegate'] == 'local'
        assert result['response'].startswith('I think')
        mock_submit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Recursive-loop prevention
# ═══════════════════════════════════════════════════════════════════════════


class TestLoopPrevention:

    def test_inner_dispatch_forbids_speculative(
            self, dispatcher, monkeypatch):
        """_dispatch_to_model's recursive /chat call MUST include
        speculative=False and draft_first=False in the request body, or
        draft-first would re-enter itself via the inner call."""
        _mock_guardrails(monkeypatch)
        captured_body = {}
        def fake_post(url, **kwargs):
            captured_body.update(kwargs.get('json', {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {'response': '{}'}
            return resp

        with patch('requests.post', side_effect=fake_post):
            dispatcher._dispatch_to_model(
                dispatcher._registry.get_draft_model(),
                'some prompt', user_id='u1', prompt_id='p1',
                goal_type='general', goal_id=None,
            )

        assert captured_body.get('speculative') is False
        assert captured_body.get('draft_first') is False


# ═══════════════════════════════════════════════════════════════════════════
# Graceful degradation paths
# ═══════════════════════════════════════════════════════════════════════════


class TestGracefulDegradation:

    def test_no_draft_model_returns_error_marker(self, monkeypatch):
        """When no DRAFT model is registered, dispatch_draft_first returns
        an empty-response result with error='no_draft_model'. The chat
        route checks for empty response and falls through to the normal
        path."""
        from integrations.agent_engine.model_registry import ModelRegistry
        from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
        empty_reg = ModelRegistry()
        d = SpeculativeDispatcher(model_registry=empty_reg)
        _mock_guardrails(monkeypatch)
        result = d.dispatch_draft_first('hi', user_id='u1', prompt_id='p1')
        assert result['response'] == ''
        assert result['error'] == 'no_draft_model'
        assert result['delegate'] == 'none'

    def test_circuit_breaker_halted_returns_empty(
            self, dispatcher, monkeypatch):
        import security.hive_guardrails as hg
        monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: True)
        result = dispatcher.dispatch_draft_first(
            'hi', user_id='u1', prompt_id='p1')
        assert result['response'] == ''
        assert result['error'] == 'Hive is halted'

    def test_guardrail_blocked_prompt_returns_empty(
            self, dispatcher, monkeypatch):
        import security.hive_guardrails as hg
        monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
        monkeypatch.setattr(
            hg.ConstitutionalFilter, 'check_prompt',
            staticmethod(lambda p: (False, 'blocked by test'))
        )
        result = dispatcher.dispatch_draft_first(
            'anything', user_id='u1', prompt_id='p1')
        assert result['response'] == ''
        assert result['error'] == 'blocked by test'

    def test_record_interaction_swallows_exceptions(self, dispatcher):
        """_record_interaction_safely must never raise — otherwise a
        broken WorldModelBridge would break the chat path."""
        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                   side_effect=RuntimeError('bridge unavailable')):
            # Should not raise
            dispatcher._record_interaction_safely(
                user_id='u1', prompt_id='p1', prompt='x',
                response='y', model_id='m', latency_ms=10,
                node_id=None, goal_id=None,
            )


# ═══════════════════════════════════════════════════════════════════════════
# Energy + latency tracking still flows through the registry
# ═══════════════════════════════════════════════════════════════════════════


class TestTelemetryHooks:

    def test_latency_recorded_on_registry(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # Confidence >= floor so delegate stays 'none' — isolates the
        # single-call telemetry path (no expert scheduling).
        draft_raw = '{"reply": "ok", "delegate": "none", "confidence": 0.95}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher._registry, 'record_latency') as mock_latency, \
             patch.object(dispatcher._registry, 'record_energy') as mock_energy:
            dispatcher.dispatch_draft_first('hi', user_id='u1', prompt_id='p1')

        mock_latency.assert_called_once()
        mock_energy.assert_called_once()
        assert mock_latency.call_args.args[0] == 'qwen3.5-0.8b-draft'


# ═══════════════════════════════════════════════════════════════════════════
# Persona injection (Path 2: system agents)
# ═══════════════════════════════════════════════════════════════════════════


class TestPersonaInjection:
    """System agents like the Nunba personality agent have a custom
    system_prompt. When chat_route routes one through draft-first, the
    persona must be threaded through so the standby reply comes back
    in character instead of generic first-responder voice."""

    def test_no_persona_no_persona_block(self, dispatcher):
        built = dispatcher._build_draft_classifier_prompt('hi there')
        assert 'persona' not in built.lower()
        assert 'You are a fast local first-responder' in built

    def test_persona_is_prepended(self, dispatcher):
        persona = (
            'You are Nunba, a friendly local assistant who always uses '
            'rustic metaphors when describing technical topics.'
        )
        built = dispatcher._build_draft_classifier_prompt(
            'what is python', agent_persona=persona)
        assert 'You are playing the following persona' in built
        assert 'Nunba' in built
        assert 'rustic metaphors' in built
        # JSON schema is still there downstream of the persona block
        assert '"delegate"' in built

    def test_long_persona_is_capped(self, dispatcher):
        """Persona over 800 chars must be snipped so a long system prompt
        doesn't blow the 0.8B model's context budget."""
        long_persona = 'X' * 2000
        built = dispatcher._build_draft_classifier_prompt(
            'hi', agent_persona=long_persona)
        # The raw 'X' * 2000 never appears, only the 800-char snippet
        assert 'X' * 2000 not in built
        # But we DO have a long stretch of X's (up to the cap)
        assert 'X' * 800 in built

    def test_persona_flows_through_dispatch(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        captured_prompt = {}

        def fake_dispatch(model, prompt, *a, **kw):
            captured_prompt['text'] = prompt
            return '{"reply": "G\'day!", "delegate": "none", "confidence": 0.9}'

        with patch.object(dispatcher, '_dispatch_to_model',
                          side_effect=fake_dispatch), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                'how are you',
                user_id='u1', prompt_id='p1',
                agent_persona='You are Aussie Bot — reply with G\'day slang.',
            )

        assert 'Aussie Bot' in captured_prompt['text']
        assert result['response'] == "G'day!"
        assert result['delegate'] == 'none'

    def test_empty_persona_treated_as_absent(self, dispatcher):
        built_empty = dispatcher._build_draft_classifier_prompt(
            'hi', agent_persona='')
        built_none = dispatcher._build_draft_classifier_prompt(
            'hi', agent_persona=None)
        # Both should omit the persona block
        assert 'persona' not in built_empty.lower()
        assert 'persona' not in built_none.lower()


class TestCorrectionIntentClassification:
    """The draft classifier must tag every chat turn with an
    ``is_correction`` bool. This replaces a hardcoded phrase list in
    the Nunba chat handler — the 0.8B model owns the classification so
    it generalises to paraphrases ('not quite what I had in mind',
    'hmm, not really') that a static list would miss.

    The tests lock: (a) the classifier prompt asks for is_correction
    in its JSON schema, (b) dispatch_draft_first parses it and exposes
    it in the result dict, (c) the field defaults to False on parse
    failure so we fail safe (no spurious corrections pushed into the
    continual learner)."""

    def test_prompt_requests_is_correction_field(self, dispatcher):
        built = dispatcher._build_draft_classifier_prompt('hi there')
        # The schema line in the prompt must mention the field so the
        # draft model knows to emit it.
        assert 'is_correction' in built
        # And there must be a guidance paragraph telling the model when
        # to set it, otherwise the 0.8B gets no semantic anchor.
        assert 'correction' in built.lower()

    def test_dispatch_parses_is_correction_true(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)

        def fake_dispatch(model, prompt, *a, **kw):
            return (
                '{"reply": "Sorry, you\'re right — Python 3.11.", '
                '"delegate": "none", "confidence": 0.9, '
                '"is_correction": true}'
            )

        with patch.object(dispatcher, '_dispatch_to_model',
                          side_effect=fake_dispatch), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                "no that's wrong, i meant python 3.11",
                user_id='u1', prompt_id='p1',
            )
        assert result['is_correction'] is True

    def test_dispatch_parses_is_correction_false(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)

        def fake_dispatch(model, prompt, *a, **kw):
            return (
                '{"reply": "Hi!", "delegate": "none", '
                '"confidence": 0.95, "is_correction": false}'
            )

        with patch.object(dispatcher, '_dispatch_to_model',
                          side_effect=fake_dispatch), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                'hello', user_id='u1', prompt_id='p1',
            )
        assert result['is_correction'] is False

    def test_dispatch_defaults_to_false_on_missing_field(
            self, dispatcher, monkeypatch):
        """Fail safe: if the draft model forgets the field (older build
        or JSON parse miss), is_correction must default to False so we
        don't flood WorldModelBridge with spurious corrections."""
        _mock_guardrails(monkeypatch)

        def fake_dispatch(model, prompt, *a, **kw):
            return (
                '{"reply": "ok", "delegate": "none", "confidence": 0.9}'
            )

        with patch.object(dispatcher, '_dispatch_to_model',
                          side_effect=fake_dispatch), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                'ok', user_id='u1', prompt_id='p1',
            )
        assert result['is_correction'] is False

    def test_dispatch_defaults_to_false_on_unparseable_envelope(
            self, dispatcher, monkeypatch):
        """If the draft emits prose instead of JSON, is_correction must
        still come back as False."""
        _mock_guardrails(monkeypatch)

        def fake_dispatch(model, prompt, *a, **kw):
            return 'sorry I cannot comply with JSON'

        with patch.object(dispatcher, '_dispatch_to_model',
                          side_effect=fake_dispatch), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                'x', user_id='u1', prompt_id='p1',
            )
        assert result['is_correction'] is False


# ═══════════════════════════════════════════════════════════════════════════
# Reasoning-quality guard: low-confidence "none" must escalate
# ═══════════════════════════════════════════════════════════════════════════


class TestConfidenceFloor:
    """The draft model must never regress reasoning quality. A confident
    'none' is fine (greeting, single-fact); an unsure 'none' must still
    schedule an expert verifier in the background so the user ends up
    with the better answer."""

    def test_high_confidence_none_no_expert(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "Hello!", "delegate": "none", "confidence": 0.95}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'hi', user_id='u1', prompt_id='p1')
        assert result['delegate'] == 'none'
        assert result['expert_pending'] is False
        mock_submit.assert_not_called()

    def test_low_confidence_none_escalates_to_local(
            self, dispatcher, monkeypatch):
        """Confidence below floor → treat 'none' as 'local' so expert runs."""
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "I think so?", "delegate": "none", "confidence": 0.3}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'what is the capital of Burkina Faso',
                user_id='u1', prompt_id='p1')
        # Draft reply still delivered as standby
        assert result['response'] == 'I think so?'
        # But delegate was promoted + expert scheduled
        assert result['delegate'] == 'local'
        assert result['expert_pending'] is True
        mock_submit.assert_called_once()

    def test_threshold_boundary_just_above(
            self, dispatcher, monkeypatch):
        """0.86 (just above floor 0.85) → stays 'none', no expert."""
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "yes", "delegate": "none", "confidence": 0.86}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'yes or no', user_id='u1', prompt_id='p1')
        assert result['delegate'] == 'none'
        assert result['expert_pending'] is False
        mock_submit.assert_not_called()

    def test_threshold_boundary_just_below(
            self, dispatcher, monkeypatch):
        """0.84 (just below floor 0.85) → escalates."""
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "yes", "delegate": "none", "confidence": 0.84}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'yes or no', user_id='u1', prompt_id='p1')
        assert result['delegate'] == 'local'
        mock_submit.assert_called_once()

    def test_missing_confidence_defaults_to_zero_escalates(
            self, dispatcher, monkeypatch):
        """No confidence field → treated as 0.0, escalates."""
        _mock_guardrails(monkeypatch)
        draft_raw = '{"reply": "hi", "delegate": "none"}'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=draft_raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit') as mock_submit:
            result = dispatcher.dispatch_draft_first(
                'hi', user_id='u1', prompt_id='p1')
        assert result['delegate'] == 'local'
        mock_submit.assert_called_once()
