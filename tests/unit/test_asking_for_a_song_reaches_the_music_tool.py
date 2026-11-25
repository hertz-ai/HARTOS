"""Asking for a song makes one; it is not answered with lyrics.

LIVE 2026-11-25 12:22 IST, installed Nunba on HARTOS 263597bda.  The person
asked: "Please sing me a short happy song, about 15 seconds, about a red
ball bouncing. Use the music generator."  The draft classifier answered

    {"delegate": "none", "is_casual": true, "confidence": 0.95, ...}

and its reply, lyrics written as text, shipped as the final answer.  The
AceStep music tool was healthy on its port and never called.

The draft has no tools.  The classifier's own media_request flag now names
what must be made, and the dispatcher's actionable-intent guard hands the
turn on (delegate='local'), so the /chat route's one reader
(escalation_reasons.draft_delegates) falls through to get_ans, whose tool
set carries the service tools (AceStep among them).
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.agent_engine.escalation_reasons import draft_delegates


@pytest.fixture
def dispatcher():
    from integrations.agent_engine.model_registry import (
        ModelRegistry, ModelBackend, ModelTier)
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='draft', display_name='Draft', tier=ModelTier.DRAFT,
        config_list_entry={'model': 'd', 'api_key': 'x',
                           'base_url': 'http://localhost:1/v1',
                           'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.45, is_local=True))
    reg.register(ModelBackend(
        model_id='fast', display_name='Fast', tier=ModelTier.FAST,
        config_list_entry={'model': 'f', 'api_key': 'x',
                           'base_url': 'http://localhost:1/v1',
                           'price': [0, 0]},
        avg_latency_ms=700.0, accuracy_score=0.6, is_local=True))
    d = SpeculativeDispatcher(model_registry=reg)
    d._health_probe_enabled = False
    return d


@pytest.fixture(autouse=True)
def _no_guardrail_network(monkeypatch):
    import security.hive_guardrails as hg
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted', lambda: False)
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_prompt',
                        staticmethod(lambda p: (True, '')))


_SONG = ("Please sing me a short happy song, about 15 seconds, about a red "
         "ball bouncing. Use the music generator.")
_LYRICS = "Here's a cheerful little tune for you: 'Bounce, bounce, go high...'"


def _run(dispatcher, prompt, envelope):
    with patch.object(dispatcher, '_dispatch_to_model',
                      return_value=envelope), \
         patch.object(dispatcher, '_record_interaction_safely'), \
         patch.object(dispatcher._expert_pool, 'submit'):
        return dispatcher.dispatch_draft_first(prompt, user_id='10202',
                                               prompt_id=None)


def test_the_classifier_is_asked_what_media_must_be_made(dispatcher):
    prompt = dispatcher._build_draft_classifier_prompt(_SONG)
    assert '"media_request"' in prompt
    assert 'media_request:' in prompt


def test_a_song_request_is_not_finished_by_the_draft(dispatcher):
    """The live envelope, plus the flag the classifier is now asked for."""
    envelope = json.dumps({'reply': _LYRICS, 'delegate': 'none',
                           'confidence': 0.95, 'is_casual': True,
                           'media_request': 'music'})
    result = _run(dispatcher, _SONG, envelope)
    assert result['delegate'] == 'local'
    assert result['escalation_reason'] == 'actionable_intent'
    assert 'Bounce' not in result['response']
    # The /chat route's one reader: True means it falls through to get_ans,
    # whose is_first tool set carries the service tools (AceStep).
    assert draft_delegates(result['delegate']) is True


@pytest.mark.parametrize('kind', ['image', 'video', 'speech'])
def test_other_made_media_hand_off_too(dispatcher, kind):
    envelope = ('{"reply": "Sure!", "delegate": "none", "confidence": 0.95, '
                '"is_casual": true, "media_request": "%s"}' % kind)
    result = _run(dispatcher, 'make me one', envelope)
    assert result['delegate'] == 'local'


@pytest.mark.parametrize('flag', ['', 'none'])
def test_how_are_you_stays_on_the_casual_path(dispatcher, flag):
    envelope = ('{"reply": "I am doing well, thanks for asking!", '
                '"delegate": "none", "confidence": 0.95, "is_casual": true, '
                '"media_request": "%s"}' % flag)
    result = _run(dispatcher, 'how are you', envelope)
    assert result['delegate'] == 'none'
    assert result['response'] == 'I am doing well, thanks for asking!'
    assert draft_delegates(result['delegate']) is False
