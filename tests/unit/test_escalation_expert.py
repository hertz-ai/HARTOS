"""#106d: a stuck goal action goes to an expert, not back to the model that
already failed it.

model_registry.get_escalation_expert picks the most accurate EXPERT-tier
backend this node can dispatch to, leaving out the model its own turns run on
(resolve_llm_backend's endpoint and model): handing the action back to that
model is a retry, not an expert.  The node's own LLM registers as
'configured-api' at FAST, so in practice the expert is Claude Code where it is
logged in or a hive peer's verified expert.  On a node with neither the action
goes to a person.

    python -m pytest tests/unit/test_escalation_expert.py --noconftest -q
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.agent_engine import model_registry as mr  # noqa: E402

OWN = ('https://llm.example/qwen', 'qwen3.8-27b')


def _backend(model_id, tier, base_url, model=None, accuracy=0.9):
    return mr.ModelBackend(
        model_id=model_id, display_name=model_id, tier=tier,
        config_list_entry={'model': model or model_id, 'base_url': base_url,
                           'api_key': 'x', 'price': [0, 0]},
        accuracy_score=accuracy)


@pytest.fixture
def registry():
    with patch.object(mr, '_own_llm_target', return_value=OWN):
        yield mr.ModelRegistry()


def test_claude_code_is_the_expert_where_it_is_logged_in(registry):
    registry.register(_backend('configured-api', mr.ModelTier.FAST,
                               'https://llm.example/qwen/v1', 'qwen3.8-27b', 0.85))
    claude = _backend('claude-code', mr.ModelTier.EXPERT,
                      'http://127.0.0.1:5000/api/claude/v1', accuracy=0.95)
    registry.register(claude)
    assert registry.get_escalation_expert() is claude


def test_no_expert_tier_means_a_person(registry):
    registry.register(_backend('configured-api', mr.ModelTier.FAST,
                               'https://llm.example/qwen/v1', 'qwen3.8-27b'))
    assert registry.get_escalation_expert() is None


def test_the_model_that_already_ran_the_turn_is_not_an_expert(registry):
    # The same endpoint and model, written with /v1 where the node's own is not.
    registry.register(_backend('hive:peer:qwen', mr.ModelTier.EXPERT,
                               'https://llm.example/qwen/v1', 'qwen3.8-27b'))
    assert registry.get_escalation_expert() is None


def test_a_backend_that_cannot_be_dialled_is_skipped(registry):
    registry.register(_backend('distributed-shard', mr.ModelTier.EXPERT,
                               'shard://cluster'))
    assert registry.get_escalation_expert() is None


def test_the_most_accurate_expert_wins(registry):
    a = _backend('hive:a', mr.ModelTier.EXPERT, 'https://a.example/v1', 'a', 0.80)
    b = _backend('hive:b', mr.ModelTier.EXPERT, 'https://b.example/v1', 'b', 0.92)
    registry.register(a)
    registry.register(b)
    assert registry.get_escalation_expert() is b


def test_the_own_target_reads_only_endpoint_and_model():
    entry = {'model': 'm', 'base_url': 'https://h.example/v1/', 'api_key': 'secret'}
    with patch('core.autogen_config.resolve_llm_backend', return_value=('api', entry)):
        assert mr._own_llm_target() == ('https://h.example', 'm')


def test_no_configured_llm_leaves_nothing_to_exclude():
    with patch('core.autogen_config.resolve_llm_backend',
               side_effect=RuntimeError('not configured')):
        assert mr._own_llm_target() == ('', '')
