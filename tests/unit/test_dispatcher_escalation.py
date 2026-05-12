"""Tests for the EscalationReason taxonomy + plumbing through
dispatch_draft_first → _schedule_expert_background → record_interaction.

Each guard in dispatch_draft_first stamps a canonical reason; the
returned dict surfaces it as the ``escalation_reason`` field, and the
``_active[speculation_id]`` entry carries the same value for admin
/diag observers.  Coverage matrix:

    Guard                          | escalation_reason value
    -------------------------------+-------------------------
    delegate='none' (no escalate)  | None  (returned dict only)
    delegate='local'/'hive' direct | classifier_delegate
    refusal pattern fires          | refusal_override
    confidence < floor             | low_confidence
    agent_bound + delegate='none'  | agent_bound
    actionable_intent flag         | actionable_intent
    envelope parse failure         | parse_failure
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def fresh_registry():
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier,
    )
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-0.8b-draft',
        display_name='Qwen3.5 0.8B (Draft)',
        tier=ModelTier.DRAFT,
        config_list_entry={
            'model': 'Qwen3.5-0.8B-Instruct', 'api_key': 'dummy',
            'base_url': 'http://localhost:8080/v1', 'price': [0, 0],
        },
        avg_latency_ms=300.0, accuracy_score=0.45, is_local=True,
    ))
    reg.register(ModelBackend(
        model_id='qwen3.5-4b-local',
        display_name='Qwen3.5 4B (Fast)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'Qwen3.5-4B', 'api_key': 'dummy',
            'base_url': 'http://localhost:8080/v1', 'price': [0, 0],
        },
        avg_latency_ms=700.0, accuracy_score=0.60, is_local=True,
    ))
    return reg


@pytest.fixture
def dispatcher(fresh_registry):
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher,
    )
    d = SpeculativeDispatcher(model_registry=fresh_registry)
    d._health_probe_enabled = False
    return d


def _mock_guardrails(monkeypatch):
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
    monkeypatch.setattr(
        hg.ConstitutionalFilter, 'check_prompt',
        staticmethod(lambda p: (True, '')),
    )


class TestEscalationReasonEnum:
    """The taxonomy itself: enum values, str inheritance, coerce()
    fallback path."""

    def test_string_inheritance_for_json_round_trip(self):
        from integrations.agent_engine.escalation_reasons import (
            EscalationReason,
        )
        # str inheritance: == works against raw strings + JSON-safe
        assert EscalationReason.CLASSIFIER_DELEGATE == 'classifier_delegate'
        assert EscalationReason.REFUSAL_OVERRIDE.value == 'refusal_override'

    def test_coerce_accepts_string_enum_and_none(self):
        from integrations.agent_engine.escalation_reasons import (
            EscalationReason, coerce,
        )
        assert coerce(None) is None
        assert coerce('') is None
        assert coerce('agent_bound') is EscalationReason.AGENT_BOUND
        assert (
            coerce(EscalationReason.LOW_CONFIDENCE)
            is EscalationReason.LOW_CONFIDENCE
        )

    def test_coerce_rejects_unknown_without_raising(self):
        """Observability metadata must never crash the chat path; an
        unknown reason value coerces to None instead of raising."""
        from integrations.agent_engine.escalation_reasons import coerce
        assert coerce('totally_made_up_reason') is None
        assert coerce(42) is None  # type-confused caller


class TestDispatchReturnDict:
    """The dispatch_draft_first return dict surfaces the reason
    additively — None when no escalation happened, the canonical
    value otherwise."""

    def test_no_escalation_returns_none_reason(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # delegate=none + high confidence + no agent_bound = draft is final
        raw = (
            '{"reply": "Hi there!", "delegate": "none", '
            '"confidence": 0.95}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely') as rec:
            result = dispatcher.dispatch_draft_first(
                'hello', user_id='u', prompt_id='p')
        assert result['escalation_reason'] is None
        # record_interaction got escalation_reason=None too
        assert rec.call_args.kwargs['escalation_reason'] is None

    def test_classifier_delegate_local(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        raw = (
            '{"reply": "Working on it", "delegate": "local", '
            '"confidence": 0.5}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely') as rec, \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'do something', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'classifier_delegate'
        assert rec.call_args.kwargs['escalation_reason'] == 'classifier_delegate'

    def test_classifier_delegate_hive(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        raw = (
            '{"reply": "Hold on", "delegate": "hive", '
            '"confidence": 0.5}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'complex query', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'classifier_delegate'

    def test_refusal_override(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        raw = (
            '{"reply": "I cannot access external URLs", '
            '"delegate": "none", "confidence": 0.9}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'fetch this URL', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'refusal_override'

    def test_low_confidence(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # delegate=none + confidence below _DRAFT_CONFIDENCE_FLOOR (0.85)
        raw = (
            '{"reply": "Maybe...", "delegate": "none", '
            '"confidence": 0.5}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'tricky question', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'low_confidence'

    def test_agent_bound(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # delegate=none + high confidence + agent_bound=True forces
        # AGENT_BOUND escalation
        raw = (
            '{"reply": "Hi!", "delegate": "none", '
            '"confidence": 0.95}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'hi', user_id='u', prompt_id='p',
                agent_bound=True)
        assert result['escalation_reason'] == 'agent_bound'

    def test_actionable_intent(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # delegate=none + classifier surfaces is_create_agent intent
        raw = (
            '{"reply": "Sure!", "delegate": "none", '
            '"confidence": 0.95, "is_create_agent": true}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'create me an agent', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'actionable_intent'

    def test_parse_failure(self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        # Unparseable envelope → defaults delegate='local' → reason
        # should be PARSE_FAILURE (not the CLASSIFIER_DELEGATE baseline)
        raw = 'this is not JSON at all and never will be'
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'anything', user_id='u', prompt_id='p')
        assert result['escalation_reason'] == 'parse_failure'


class TestActiveEntryReason:
    """The reason is stamped into self._active so admin /diag observers
    can read the speculation entry without re-deriving heuristics."""

    def test_reason_in_active_entry_on_classifier_delegate(
            self, dispatcher, monkeypatch):
        _mock_guardrails(monkeypatch)
        raw = (
            '{"reply": "ok", "delegate": "local", '
            '"confidence": 0.5}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'), \
             patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            result = dispatcher.dispatch_draft_first(
                'do x', user_id='u', prompt_id='p')

        speculation_id = result['speculation_id']
        assert speculation_id in dispatcher._active
        entry = dispatcher._active[speculation_id]
        assert entry.get('escalation_reason') == 'classifier_delegate'
        assert entry.get('delegate') == 'local'

    def test_reason_absent_from_active_when_no_escalation(
            self, dispatcher, monkeypatch):
        """delegate='none' (no escalation) means _schedule_expert_background
        was never called — speculation_id must not appear in _active."""
        _mock_guardrails(monkeypatch)
        raw = (
            '{"reply": "Hi!", "delegate": "none", '
            '"confidence": 0.95}'
        )
        with patch.object(dispatcher, '_dispatch_to_model',
                          return_value=raw), \
             patch.object(dispatcher, '_record_interaction_safely'):
            result = dispatcher.dispatch_draft_first(
                'hi', user_id='u', prompt_id='p')
        assert result['speculation_id'] not in dispatcher._active


class TestScheduleExpertBackgroundReasonPlumb:
    """Direct test of the schedule helper's contract: the reason kwarg
    is optional + stored in the active entry when set."""

    def test_reason_kwarg_stored_in_active(
            self, dispatcher, fresh_registry):
        from integrations.agent_engine.escalation_reasons import (
            EscalationReason,
        )
        with patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            ok = dispatcher._schedule_expert_background(
                speculation_id='abcd-1234',
                prompt='p', fast_response='r',
                expert_model=fresh_registry.get_fast_model(),
                user_id='u', prompt_id='pid',
                goal_id=None, goal_type='general',
                origin_model_id='qwen3.5-0.8b-draft',
                origin_model_role='draft_model',
                delegate='local',
                escalation_reason=EscalationReason.LOW_CONFIDENCE,
            )
        assert ok is True
        entry = dispatcher._active['abcd-1234']
        assert entry['escalation_reason'] == 'low_confidence'

    def test_reason_kwarg_optional_legacy_signature(
            self, dispatcher, fresh_registry):
        """dispatch_speculative call site doesn't pass escalation_reason
        — confirm the entry has no key (not a stray None)."""
        with patch.object(dispatcher, '_check_and_reserve_budget',
                          return_value=True), \
             patch.object(dispatcher._expert_pool, 'submit'):
            ok = dispatcher._schedule_expert_background(
                speculation_id='legacy-1',
                prompt='p', fast_response='r',
                expert_model=fresh_registry.get_fast_model(),
                user_id='u', prompt_id='pid',
                goal_id=None, goal_type='general',
                origin_model_id='qwen3.5-0.8b-draft',
                origin_model_role='fast_model',
            )
        assert ok is True
        entry = dispatcher._active['legacy-1']
        assert 'escalation_reason' not in entry
