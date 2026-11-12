"""A goal's Claude turn is not refused by a fast turn the dispatcher abandoned.

Measured on central 2026-11-12 03:42-03:56Z (build f46f0ac6b), the first two
of the eight four-business goals resumed that morning:

  * the daemon's speculative branch posted each goal's turn to /chat on the
    fast model (openrouter/free) and stopped waiting after 30 s.  The turns
    ran 153.7 s and 223.6 s and kept running on the server; the daemon
    counted "the fast model returned no reply" as a failed turn.
  * the Claude expert was then scheduled for the same conversation.  The
    TURN-MODEL guard (hartos.helper.bind_agents_to_turn_model, #172) refused
    it after 5 calls: "another turn of this group is running on another
    model".  0 calls reached api.anthropic.com until 03:46.
  * _run_collapsed_expert_path published the refusal sentence
    (held_for_another_turn_reply) to the goal owner's chat as the expert's
    answer, twice, and recorded it for continual learning, improved=True.
  * a later Claude turn ran 462.9 s; the expert leg stopped waiting at
    120 s and its answer was lost.

Now (review of f523d160c, MF1-MF3): a goal's fast turn and its expert turn
are waited for as long as they run, up to the watchdog's bound for one LLM
call (security.node_watchdog.LLM_CALL_TIMEOUT_SECONDS), with the daemon
thread marked as in an LLM call while its fast turn runs.  A goal turn
still running past that has no result yet: no expert is scheduled behind
it and the daemon holds the goal with no strike.  A person's own fast leg
(the draft classifier, a person's speculative turn) keeps its 30 s.  The
collapsed path judges the expert's reply by dispatch's one "not a result"
rule (_turn_failure): a turn that did not run is not delivered or recorded.

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
GOAL = '37177673-0622'
PLAN = 'Plan ready: 3 prospects listed.'


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
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted',
                        staticmethod(lambda: False))
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='draft-local', display_name='Draft',
        tier=ModelTier.DRAFT,
        config_list_entry={'model': 'Qwen3.5-0.8B', 'api_key': 'dummy',
                           'base_url': 'http://localhost:8081/v1',
                           'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.4, is_local=True,
    ))
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


@pytest.fixture
def watchdog_marks():
    """The two watchdog markers, recorded in order with each POST."""
    events = []
    with patch('integrations.agent_engine.dispatch._notify_watchdog_llm_start',
               side_effect=lambda: events.append('marked')), \
            patch('integrations.agent_engine.dispatch._notify_watchdog_llm_end',
                  side_effect=lambda: events.append('cleared')), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value={}):
        yield events


def _server_taking(seconds, reply=PLAN, events=None):
    """requests.post for a /chat turn that takes ``seconds``: a client that
    waits less gets requests' own ReadTimeout, as on the wire."""
    seen = {'timeouts': []}

    def post(url, **kwargs):
        seen['timeouts'].append(kwargs.get('timeout'))
        if events is not None:
            events.append('post')
        if kwargs.get('timeout') is not None and kwargs['timeout'] < seconds:
            raise requests.exceptions.ReadTimeout(
                f"Read timed out. (read timeout={kwargs['timeout']})")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'response': reply}
        return resp
    return post, seen


def _watchdog_bound():
    from security.node_watchdog import LLM_CALL_TIMEOUT_SECONDS
    return LLM_CALL_TIMEOUT_SECONDS


def _speculate(dispatcher, goal_id=GOAL):
    """The daemon's call (agent_daemon's speculative branch) or, with no
    goal_id, a person's speculative /chat turn."""
    with patch.object(dispatcher, '_schedule_expert_background',
                      return_value=True) as schedule:
        out = dispatcher.dispatch_speculative(
            'Find speech therapy centres', 'owner', None, goal_id, 'outreach')
    return out, schedule


# ─── A goal's fast turn is waited for ───────────────────────────────────

def test_a_goal_turn_of_223_s_returns_its_reply_and_then_its_expert_runs(
        dispatcher, http_mode, watchdog_marks):
    post, seen = _server_taking(223.6)
    with patch('requests.post', side_effect=post):
        out, schedule = _speculate(dispatcher)
    assert out['response'] == PLAN
    assert seen['timeouts'] == [_watchdog_bound()]
    schedule.assert_called_once()
    assert schedule.call_args.kwargs['fast_response'] == PLAN


def test_the_daemon_thread_is_marked_in_an_llm_call_around_the_goal_turn(
        dispatcher, http_mode, watchdog_marks):
    """Unmarked, the watchdog calls the daemon thread frozen after 300 s.
    The mark is cleared even when the turn outlives the wait."""
    post, _ = _server_taking(223.6, events=watchdog_marks)
    with patch('requests.post', side_effect=post):
        _speculate(dispatcher)
    assert watchdog_marks == ['marked', 'post', 'cleared']

    watchdog_marks.clear()
    post, _ = _server_taking(10_000, events=watchdog_marks)
    with patch('requests.post', side_effect=post):
        _speculate(dispatcher)
    assert watchdog_marks == ['marked', 'post', 'cleared']


def test_a_goal_turn_still_running_is_held_with_no_expert_behind_it(
        dispatcher, http_mode, watchdog_marks):
    from integrations.agent_engine.dispatch import (
        dispatch_failure_reason, is_transient_deferral,
        speculative_turn_result)
    post, _ = _server_taking(10_000)
    with patch('requests.post', side_effect=post):
        out, schedule = _speculate(dispatcher)
    schedule.assert_not_called()
    assert out['still_running'] is True
    assert out['response'] == '' and out['expert_pending'] is False

    # What the daemon then does with it (agent_daemon's speculative branch
    # and its failure arm): no result, held, no strike.
    assert speculative_turn_result(GOAL, out) is None
    assert is_transient_deferral(GOAL) is True
    assert dispatch_failure_reason(GOAL).startswith('deferred:')


def test_a_goal_turn_that_fails_is_still_a_failure(dispatcher, http_mode,
                                                  watchdog_marks):
    from integrations.agent_engine.dispatch import (
        dispatch_failure_reason, speculative_turn_result)
    resp = MagicMock()
    resp.status_code = 500
    with patch('requests.post', return_value=resp):
        out, _ = _speculate(dispatcher)
    assert 'still_running' not in out
    assert speculative_turn_result(GOAL, out) is None
    assert dispatch_failure_reason(GOAL).startswith('turn failed:')


# ─── A person's fast leg keeps its short wait ───────────────────────────

def test_a_persons_speculative_turn_keeps_its_30_s(dispatcher, http_mode,
                                                  watchdog_marks):
    post, seen = _server_taking(45)
    with patch('requests.post', side_effect=post):
        out, schedule = _speculate(dispatcher, goal_id=None)
    assert seen['timeouts'] == [30]
    assert out['response'] == ''
    assert 'still_running' not in out
    assert watchdog_marks == [], 'a person turn runs on a request thread'
    schedule.assert_called_once()


def test_the_draft_classifier_keeps_its_30_s(dispatcher, http_mode,
                                            watchdog_marks):
    """The classifier runs synchronously inside a person's casual /chat
    turn: the person waits for it."""
    post, seen = _server_taking(
        1, reply='{"reply": "Hello!", "delegate": "none", "confidence": 0.95}')
    with patch('requests.post', side_effect=post), \
            patch.object(dispatcher, '_schedule_expert_background',
                         return_value=False), \
            patch.object(dispatcher, '_record_interaction_safely'):
        dispatcher.dispatch_draft_first('hi', 'u1', None)
    assert seen['timeouts'][0] == 30
    assert watchdog_marks == []


def test_a_persons_fast_leg_given_up_on_says_so_at_warning(dispatcher,
                                                          http_mode,
                                                          watchdog_marks,
                                                          caplog):
    """DEBUG is not in central's log: the abandoned turn was invisible and
    the daemon's own line could only say the model 'returned no reply'."""
    post, _ = _server_taking(10_000)
    with patch('requests.post', side_effect=post), \
            caplog.at_level(logging.WARNING, logger='hevolve_social'):
        out = dispatcher._dispatch_to_model(
            dispatcher._registry.get_fast_model(), 'hi', user_id='u1',
            prompt_id=None, goal_type='general')
    assert out == ''
    lines = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING]
    assert any('configured-api' in m and 'Read timed out' in m for m in lines), lines


# ─── The expert leg ─────────────────────────────────────────────────────

def _expert(dispatcher, goal_id):
    return dispatcher._dispatch_expert_langchain(
        dispatcher._registry.get_fast_model(), 'Find speech therapy centres',
        'owner', None, 'outreach', goal_id)


def test_a_goal_expert_turn_of_463_s_is_waited_for(dispatcher, http_mode,
                                                  watchdog_marks):
    post, seen = _server_taking(462.9)
    with patch('requests.post', side_effect=post):
        assert _expert(dispatcher, GOAL) == PLAN
    assert seen['timeouts'] == [_watchdog_bound()]


def test_a_persons_expert_keeps_the_agent_turn_budget(dispatcher, http_mode,
                                                     watchdog_marks):
    from integrations.channels.chat_contract import agent_turn_timeout
    post, seen = _server_taking(462.9)
    with patch('requests.post', side_effect=post):
        assert _expert(dispatcher, None) == ''
    assert seen['timeouts'] == [agent_turn_timeout()]


# ─── The collapsed expert path: a turn that did not run is no answer ────

def _run_expert(dispatcher, expert_reply):
    expert = dispatcher._registry.get_fast_model()
    with patch.object(dispatcher, '_dispatch_expert_langchain',
                      return_value=expert_reply), \
            patch.object(dispatcher, '_deliver_expert_response') as deliver, \
            patch.object(dispatcher, '_record_interaction_safely') as record:
        dispatcher._run_collapsed_expert_path(
            'spec-held', 'Find speech therapy centres', STANDBY, expert,
            user_id='owner', prompt_id=None, goal_id=GOAL,
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


def test_the_held_sentence_never_reaches_the_owner_as_an_answer(dispatcher):
    """The measured case: the refusal published to the owner's chat."""
    from core.agent_tools import held_for_another_turn_reply
    held = held_for_another_turn_reply()
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
