"""A failed turn is never recorded as work.

The pipeline does not raise when its LLM call fails: user_facing_error() turns
the exception into a polite sentence and the turn returns it as the reply.
dispatch_goal handed that sentence back as the goal's response, so every
caller counted it: a parallel subtask was marked COMPLETED and unblocked its
dependents, the daemon cleared the goal's backoff, the marketplace recorded
distributions, welcomes and benchmarks that never happened, and the
instruction queue completed an instruction with the failure as its result.
On central on 2026-09-14 every hosted call returned 402, so every turn failed,
and nothing downstream noticed.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.agent_tools import _COULD_NOT_FINISH_PREFIX, _SNAG_REPLY
from integrations.agent_engine import dispatch as dispatch_mod

FAILED = f"{_COULD_NOT_FINISH_PREFIX}Error code: 402 - insufficient_quota"


def _gates(chat_reply):
    """Stand-ins that let dispatch_goal reach Tier 1 (the shape
    test_dispatch.py uses), with the in-process /chat answering chat_reply."""
    guardrails = MagicMock()
    guardrails.GuardrailEnforcer.before_dispatch.return_value = (
        True, 'ok', 'the prompt')
    guardrails.GuardrailEnforcer.after_response.return_value = (True, 'ok')
    return {
        'integrations.agent_engine.budget_gate': MagicMock(
            pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))),
        'security.hive_guardrails': guardrails,
        'security.immutable_audit_log': MagicMock(
            get_audit_log=MagicMock(return_value=MagicMock())),
        'routes.hartos_backend_adapter': MagicMock(
            chat=MagicMock(return_value={'text': chat_reply})),
    }


def _through_tier1(chat_reply):
    """The context a real dispatch_goal call runs in, with chat_reply as the
    turn's reply."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.dict('sys.modules', _gates(chat_reply)))
    stack.enter_context(patch.object(
        dispatch_mod, 'is_user_recently_active', return_value=False))
    stack.enter_context(patch.object(
        dispatch_mod, '_get_distributed_coordinator', return_value=None))
    return stack


def _dispatch(chat_reply, goal_id):
    with _through_tier1(chat_reply):
        return dispatch_mod.dispatch_goal('the prompt', 'u1', goal_id,
                                          'marketing')


# ── dispatch_goal ──

@pytest.mark.parametrize('reply', [FAILED, _SNAG_REPLY])
def test_a_failed_turn_is_no_response_and_says_why(reply):
    assert _dispatch(reply, 'g-failed') is None
    assert dispatch_mod.dispatch_failure_reason('g-failed').startswith(
        'turn failed: ')
    # Read once.
    assert dispatch_mod.dispatch_failure_reason('g-failed') is None


def test_a_real_reply_is_the_response():
    assert _dispatch('Posted the launch thread.', 'g-real') == \
        'Posted the launch thread.'
    assert dispatch_mod.dispatch_failure_reason('g-real') is None


def test_a_reason_describes_only_the_latest_call():
    _dispatch(FAILED, 'g-twice')
    _dispatch('Done.', 'g-twice')
    assert dispatch_mod.dispatch_failure_reason('g-twice') is None


def test_a_failed_turn_over_http_is_not_training_data():
    bridge = MagicMock()
    reply = SimpleNamespace(status_code=200,
                            json=lambda: {'response': FAILED})
    modules = _gates('unused')
    modules['integrations.agent_engine.world_model_bridge'] = MagicMock(
        get_world_model_bridge=MagicMock(return_value=bridge))
    with patch.dict('sys.modules', modules), \
            patch.object(dispatch_mod, 'is_user_recently_active',
                         return_value=False), \
            patch.object(dispatch_mod, '_get_distributed_coordinator',
                         return_value=None), \
            patch.object(dispatch_mod, 'local_chat_dispatch',
                         return_value=('unavailable', None)), \
            patch.object(dispatch_mod, '_cb_is_open', return_value=False), \
            patch.object(dispatch_mod, '_local_dispatch_base_url',
                         return_value='http://127.0.0.1:1'), \
            patch.object(dispatch_mod, '_internal_auth_headers',
                         return_value={}), \
            patch.object(dispatch_mod, 'pooled_post', return_value=reply):
        assert dispatch_mod.dispatch_goal('p', 'u1', 'g-http',
                                          'marketing') is None
    bridge.record_interaction.assert_not_called()
    assert dispatch_mod.dispatch_failure_reason('g-http').startswith(
        'turn failed: ')


# ── The instruction queue ──

_INSTRUCTION = SimpleNamespace(id='inst-0001', text='do the thing')


def test_a_failed_turn_does_not_complete_an_instruction():
    with patch.object(dispatch_mod, 'local_chat_dispatch',
                      return_value=('ok', FAILED)):
        iid, result, error = dispatch_mod._dispatch_single_instruction(
            'http://127.0.0.1:1', 'u1', _INSTRUCTION, 'batch1')
    assert (iid, result) == ('inst-0001', None)
    assert error.startswith('turn failed: ')


def test_a_failed_turn_over_http_does_not_complete_an_instruction():
    reply = SimpleNamespace(status_code=200,
                            json=lambda: {'response': _SNAG_REPLY})
    with patch.object(dispatch_mod, 'local_chat_dispatch',
                      return_value=('unavailable', None)), \
            patch.object(dispatch_mod, '_internal_auth_headers',
                         return_value={}), \
            patch.object(dispatch_mod, 'pooled_post', return_value=reply):
        iid, result, error = dispatch_mod._dispatch_single_instruction(
            'http://127.0.0.1:1', 'u1', _INSTRUCTION, 'batch1')
    assert result is None
    assert error.startswith('turn failed: ')


def test_a_real_reply_completes_an_instruction():
    with patch.object(dispatch_mod, 'local_chat_dispatch',
                      return_value=('ok', 'Done.')):
        assert dispatch_mod._dispatch_single_instruction(
            'http://127.0.0.1:1', 'u1', _INSTRUCTION, 'batch1') == \
            ('inst-0001', 'Done.', None)


# ── A parallel subtask ──

def test_a_failed_parallel_subtask_is_failed_not_completed():
    pytest.importorskip('agent_ledger', reason='agent_ledger not installed')
    from agent_ledger import SmartLedger, Task, TaskStatus, TaskType
    from agent_ledger.backends import InMemoryBackend
    from agent_ledger.core import ExecutionMode
    from integrations.agent_engine.agent_daemon import AgentDaemon

    ledger = SmartLedger(agent_id='test', session_id='failed-turn',
                         backend=InMemoryBackend())
    ledger.add_task(Task(task_id='root-ft', description='Root goal',
                         task_type=TaskType.AUTONOMOUS,
                         execution_mode=ExecutionMode.SEQUENTIAL))
    siblings = ledger.create_sibling_tasks(
        parent_task_id='root-ft', sibling_descriptions=['A', 'B'],
        task_type=TaskType.PRE_ASSIGNED)
    for sibling in siblings:
        sibling.execution_mode = ExecutionMode.PARALLEL
        sibling.pending_reason = 'ready'
        if sibling.task_id not in ledger.task_order:
            ledger.task_order.append(sibling.task_id)

    daemon = AgentDaemon()
    goal = SimpleNamespace(id='g-parallel', goal_type='marketing',
                           user_id='u1')
    with _through_tier1(FAILED), \
            patch.object(daemon, '_get_goal_ledger', return_value=ledger):
        daemon._try_parallel_dispatch(
            goal, [{'user_id': 'a1'}, {'user_id': 'a2'}], 0, 10)

    assert {ledger.tasks[s.task_id].status for s in siblings} == \
        {TaskStatus.FAILED}


# ── The marketplace ──

@pytest.fixture
def marketplace(tmp_path, monkeypatch):
    from integrations.agent_engine import app_marketplace as market
    monkeypatch.setattr(market, '_MARKETPLACE_DIR', str(tmp_path))
    monkeypatch.setattr(market, '_LISTINGS_PATH',
                        str(tmp_path / 'listings.json'))
    place = market.AppMarketplace()
    place._save_listings({'L1': {
        'name': 'Tutor', 'description': 'A tutor app', 'owner_id': 'o1',
        'recipe_id': 'r1', 'tagline': 'Learn anything'}})
    return place, market.AppPromotionAgent(place)


def _no_response():
    return patch('integrations.agent_engine.dispatch.dispatch_goal',
                 return_value=None)


def test_a_failed_distribution_records_no_channel(marketplace):
    place, _ = marketplace
    with _no_response():
        out = place.distribute_to_channel('L1', 'telegram')
    assert 'error' in out
    assert 'telegram' not in place._listings()['L1'].get(
        'distribution_channels', [])


def test_a_failed_welcome_is_not_sent(marketplace):
    _, promoter = marketplace
    with _no_response():
        out = promoter.auto_onboard_users('L1', 'u1')
    welcome = [a for a in out['actions'] if a['type'] == 'welcome_sent']
    assert welcome and welcome[0]['success'] is False


def test_a_failed_benchmark_is_failed(marketplace):
    _, promoter = marketplace
    with _no_response():
        out = promoter.run_benchmark_comparison(['L1'])
    assert out['benchmarks'][0]['status'] == 'failed'


def test_a_failed_repromotion_is_not_scheduled(marketplace):
    _, promoter = marketplace
    with _no_response():
        assert promoter._schedule_repromotion('L1') == 'dispatch_failed'
