"""A failed expert turn is not delivered or recorded as the expert's answer.

Measured on central 9ce6023f (task #70): every spec_expert TTS marker since
boot (30) carried "I couldn't finish that: 'NoneType' object is not
iterable", the sentence create_recipe.get_response_group answers with when
its turn raises (core.agent_tools.user_facing_error).  The collapsed expert
path saw a non-empty string, spoke it over the draft, recorded it for
continual learning and closed the speculation improved=True.

The recogniser already existed (core.agent_tools.is_user_facing_error, the
one reader of the strings user_facing_error writes); the collapsed path now
asks it before delivering, and treats a failed turn like an empty one.

    pytest tests/unit/test_collapsed_expert_failed_turn.py -q
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
def dispatcher(monkeypatch):
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier,
    )
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher,
    )
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_prompt',
                        staticmethod(lambda p: (True, '')))
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='qwen3.5-4b-local', display_name='Qwen3.5 4B (Fast)',
        tier=ModelTier.FAST,
        config_list_entry={'model': 'Qwen3.5-4B', 'api_key': 'dummy',
                           'base_url': 'http://localhost:8080/v1',
                           'price': [0, 0]},
        avg_latency_ms=700.0, accuracy_score=0.60, is_local=True,
    ))
    d = SpeculativeDispatcher(model_registry=reg)
    d._health_probe_enabled = False
    return d


def _run(dispatcher, expert_reply):
    expert = dispatcher._registry.get_fast_model()
    with patch.object(dispatcher, '_dispatch_expert_langchain',
                      return_value=expert_reply), \
            patch.object(dispatcher, '_deliver_expert_response') as deliver, \
            patch.object(dispatcher, '_record_interaction_safely') as record:
        dispatcher._run_collapsed_expert_path(
            'spec-70', 'what is the capital of France', 'One moment...',
            expert, user_id='u1', prompt_id='p1', goal_id=None,
            goal_type=None)
    return deliver, record, dispatcher._results['spec-70']


def _failed_turn_replies():
    from core.agent_tools import user_facing_error
    from core.constants import BUILD_INCOMPLETE_REPLY, LLM_GENERIC_ERROR_REPLY
    return [
        user_facing_error(TypeError("'NoneType' object is not iterable")),
        user_facing_error(RuntimeError('x' * 300)),       # the snag sentence
        LLM_GENERIC_ERROR_REPLY,
        BUILD_INCOMPLETE_REPLY,
    ]


@pytest.mark.parametrize('reply', _failed_turn_replies())
def test_a_failed_turn_is_neither_delivered_nor_recorded(dispatcher, reply):
    deliver, record, result = _run(dispatcher, reply)
    deliver.assert_not_called()
    record.assert_not_called()
    assert result['improved'] is False
    assert result['expert_failed'] is True
    assert result['response'] == 'One moment...', 'the draft standby stays'


def test_a_real_answer_is_still_delivered_and_recorded(dispatcher):
    deliver, record, result = _run(dispatcher, 'Paris is the capital of France.')
    deliver.assert_called_once()
    assert deliver.call_args.args[-1] == 'Paris is the capital of France.'
    record.assert_called_once()
    assert record.call_args.kwargs['response'] == 'Paris is the capital of France.'
    assert result['improved'] is True
    assert 'expert_failed' not in result
