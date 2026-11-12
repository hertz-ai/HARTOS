"""A goal's Claude turn is not refused by a fast turn the dispatcher abandoned.

Measured on central 2026-11-12 03:42-03:44Z (build f46f0ac6b), the first two
of the eight four-business goals resumed that morning:

  * the daemon's speculative branch posted each goal's turn to /chat on the
    fast model (openrouter/free) with a 30 s timeout.  The turn (a full
    CREATE plan) ran longer, so the dispatcher got no reply, logged nothing
    above DEBUG, and the daemon counted "the fast model returned no reply" as
    a failed turn.  The turn itself kept running on the server.
  * the Claude expert was then scheduled for the same conversation.  The
    TURN-MODEL guard (hartos.helper.bind_agents_to_turn_model, #172) refused
    it: "another turn of this group is running on another model, so this
    turn does not run".  0 calls reached api.anthropic.com.
  * _run_collapsed_expert_path took the refusal sentence
    (held_for_another_turn_reply, 165 characters) as the expert's answer and
    published it to the goal owner's chat ("Expert chat publish ... len=165",
    twice), recorded it for continual learning and closed the speculation
    improved=True.

The fast leg now waits the one agent-turn budget every other /chat client of
a full turn waits (chat_contract.agent_turn_timeout, already the expert leg's
and dispatch_goal's 120 s), and says at WARNING when it gives up.  The
collapsed path judges the expert's reply by dispatch's one "not a result"
rule (_turn_failure): a turn that did not run (held, paused for the person,
waiting for the person's reply) is not delivered or recorded either.

    pytest tests/unit/test_expert_is_not_refused_by_an_abandoned_fast_turn.py -q
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

STANDBY = 'One moment...'


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
        model_id='configured-api', display_name='Configured API (fast)',
        tier=ModelTier.FAST,
        config_list_entry={'model': 'openrouter/free', 'api_key': 'dummy',
                           'base_url': 'https://openrouter.ai/api/v1',
                           'price': [0, 0]},
        avg_latency_ms=900.0, accuracy_score=0.5, is_local=True,
    ))
    d = SpeculativeDispatcher(model_registry=reg)
    d._health_probe_enabled = False
    return d


@pytest.fixture
def http_mode(monkeypatch):
    """Central's mode: an external HARTOS server, not bundled Nunba."""
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.delenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', raising=False)


def _server_taking(seconds, reply='Plan ready: 3 prospects listed.'):
    """requests.post for a /chat turn that takes ``seconds``: a client that
    waits less gets requests' own Timeout, as on the wire."""
    seen = {}

    def post(url, **kwargs):
        seen['timeout'] = kwargs.get('timeout')
        if kwargs.get('timeout') is not None and kwargs['timeout'] < seconds:
            raise requests.exceptions.ReadTimeout(
                f"Read timed out. (read timeout={kwargs['timeout']})")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'response': reply}
        return resp
    return post, seen


def _fast(dispatcher):
    return dispatcher._dispatch_to_model(
        dispatcher._registry.get_fast_model(), 'Find speech therapy centres',
        user_id='owner', prompt_id=None, goal_type='outreach',
        goal_id='37177673-0622')


# ─── The fast leg waits for its turn ────────────────────────────────────

def test_a_fast_turn_longer_than_30_s_returns_its_reply(dispatcher, http_mode):
    post, seen = _server_taking(45)
    with patch('requests.post', side_effect=post), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value={}):
        out = _fast(dispatcher)
    assert out == 'Plan ready: 3 prospects listed.'
    assert seen['timeout'] == 120, 'the default agent-turn budget'


def test_the_fast_leg_waits_the_one_agent_turn_budget(dispatcher, http_mode,
                                                      monkeypatch):
    from integrations.channels.chat_contract import agent_turn_timeout
    monkeypatch.setenv('HEVOLVE_CHANNEL_AGENT_TIMEOUT', '400')
    post, seen = _server_taking(300)
    with patch('requests.post', side_effect=post), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value={}):
        out = _fast(dispatcher)
    assert seen['timeout'] == agent_turn_timeout() == 400
    assert out == 'Plan ready: 3 prospects listed.'


def test_a_fast_turn_given_up_on_says_so_at_warning(dispatcher, http_mode,
                                                    caplog):
    """Central logs nothing below WARNING after boot: a DEBUG line left the
    abandoned turn invisible, and the daemon's own line could only say the
    model 'returned no reply'."""
    post, _ = _server_taking(10_000)
    with patch('requests.post', side_effect=post), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value={}), \
            caplog.at_level(logging.WARNING, logger='hevolve_social'):
        out = _fast(dispatcher)
    assert out == ''
    lines = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING]
    assert any('configured-api' in m and 'Read timed out' in m for m in lines), lines


# ─── The collapsed expert path: a turn that did not run is no answer ────

def _run_expert(dispatcher, expert_reply):
    expert = dispatcher._registry.get_fast_model()
    with patch.object(dispatcher, '_dispatch_expert_langchain',
                      return_value=expert_reply), \
            patch.object(dispatcher, '_deliver_expert_response') as deliver, \
            patch.object(dispatcher, '_record_interaction_safely') as record:
        dispatcher._run_collapsed_expert_path(
            'spec-held', 'Find speech therapy centres', STANDBY, expert,
            user_id='owner', prompt_id=None, goal_id='37177673-0622',
            goal_type='outreach')
    return deliver, record, dispatcher._results['spec-held']


def _turns_that_did_not_run():
    from core.agent_tools import (held_for_another_turn_reply,
                                  user_pause_reply, waiting_for_person_reply)
    return [held_for_another_turn_reply(), user_pause_reply(2),
            waiting_for_person_reply(3)]


@pytest.mark.parametrize('reply', _turns_that_did_not_run(),
                         ids=['held_for_another_turn', 'paused_for_the_person',
                              'waiting_for_the_person'])
def test_an_expert_turn_that_did_not_run_is_neither_delivered_nor_recorded(
        dispatcher, reply):
    deliver, record, result = _run_expert(dispatcher, reply)
    deliver.assert_not_called()
    record.assert_not_called()
    assert result['improved'] is False
    assert result['expert_held'] is True
    assert 'expert_failed' not in result
    assert result['response'] == STANDBY, 'the draft standby stays'


def test_the_held_sentence_never_reaches_the_person_as_an_answer(dispatcher):
    """The measured case end to end in this method: the 165-character
    refusal published to the owner's chat."""
    from core.agent_tools import held_for_another_turn_reply
    held = held_for_another_turn_reply()
    assert len(held) == 165
    deliver, _, _ = _run_expert(dispatcher, held)
    assert all(held not in str(c) for c in deliver.call_args_list)


def test_a_real_expert_answer_is_still_delivered(dispatcher):
    deliver, record, result = _run_expert(
        dispatcher, 'Listed 3 speech therapy centres in Chennai and asked the '
                    'owner for the HertzAI mailer sender.')
    deliver.assert_called_once()
    record.assert_called_once()
    assert result['improved'] is True
    assert 'expert_held' not in result and 'expert_failed' not in result
