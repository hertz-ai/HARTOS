"""A failed LLM turn must never be recorded as a hive task's result.

Measured on central 2026-09-13, minutes after the three hive task sets were
healed and became claimable for the first time since 09-01. Two of them were
claimed and marked COMPLETED with these as their results:

    I couldn't finish that: Error code: 429 - {... 'rate_limit_exceeded' ...}
    I couldn't finish that: Error code: 400 - {... 'invalid request' ...}

The pipeline does not raise when the model call fails. user_facing_error()
turns the exception into a polite sentence and returns it as the reply, which
is right for a person and wrong for a caller deciding whether work was done.
The worker handed that sentence to submit_result, the coordinator completed
the task and hashed it, and the work was lost.

Second defect on the same path: when execution produced nothing, the worker
logged and moved on without releasing its claim. On a Redis-backed node the
heartbeat keeps renewing that lock, so orphan recovery (which requires the lock
to be gone) never fires and the task is stuck IN_PROGRESS for good.

Run:
  pytest tests/unit/test_worker_failed_turn_is_not_a_result.py -q
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402


# --------------------------------------------------------------------------
# Recognising a failed turn
# --------------------------------------------------------------------------

def test_both_shapes_user_facing_error_produces_are_recognised():
    from core.agent_tools import is_user_facing_error, user_facing_error
    rate_limited = user_facing_error(Exception(
        "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"))
    snag = user_facing_error(Exception('Connection reset by peer'))
    assert rate_limited.startswith("I couldn't finish that:")
    assert is_user_facing_error(rate_limited)
    assert is_user_facing_error(snag)


def test_the_canonical_llm_error_replies_are_recognised():
    from core import constants as C
    from core.agent_tools import is_user_facing_error
    assert is_user_facing_error(C.LLM_LOADING_REPLY)
    assert is_user_facing_error(C.LLM_GENERIC_ERROR_REPLY)


def test_a_real_answer_is_not_mistaken_for_a_failure():
    """The first text is the real result Guardian Convergence produced on
    central the same morning; retrying it would have been a mistake."""
    from core.agent_tools import is_user_facing_error
    real = ("Action 1 is non-blocking and can be completed autonomously by "
            "querying long-term memory for prior Guardian Convergence "
            "commitments and human wellness metrics.")
    for reply in (real, '', '   ', None, 42):
        assert not is_user_facing_error(reply), repr(reply)


# --------------------------------------------------------------------------
# The worker does not submit a failed turn, and releases what it claimed
# --------------------------------------------------------------------------

class _Task:
    def __init__(self, task_id='t1'):
        self.task_id = task_id
        self.description = 'recruit compute'
        self.context = {'hop': 0}


def _loop():
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    return DistributedWorkerLoop()


def test_after_response_drops_an_error_reply():
    loop = _loop()
    with patch('security.hive_guardrails.GuardrailEnforcer.after_response',
               return_value=(True, '')):
        got = loop._after_response(
            "I couldn't finish that: Error code: 429 - rate limit exceeded",
            _Task())
    assert got is None, 'an error reply was passed on as a task result'


def test_after_response_still_passes_a_real_answer_through():
    loop = _loop()
    real = 'Found three GPU owners on HN asking about running models locally.'
    with patch('security.hive_guardrails.GuardrailEnforcer.after_response',
               return_value=(True, '')), \
         patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=MagicMock()):
        got = loop._after_response(real, _Task())
    assert got == real


def test_a_failed_execution_releases_its_claim_instead_of_submitting():
    loop = _loop()
    coord = MagicMock()
    coord.claim_next_task.return_value = _Task('t-fail')
    with patch.object(loop, '_get_coordinator', return_value=coord), \
         patch.object(loop, '_execute_task', return_value=None):
        loop._tick()
    coord.submit_result.assert_not_called()
    coord.abandon_task.assert_called_once_with('t-fail', loop._node_id)


# --------------------------------------------------------------------------
# The coordinator side: released now, retried later by orphan recovery
# --------------------------------------------------------------------------

class _MemBackend:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _coordinator():
    from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    led = SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())
    co = DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())
    co.submit_goal('obj', [{'task_id': 'g_task_0', 'description': 'd'}], {},
                   goal_id='g')
    return led, co


def test_an_abandoned_task_is_released_and_later_retried():
    from integrations.distributed_agent import task_coordinator as tc
    led, co = _coordinator()

    got = co.claim_next_task('worker_a')
    assert got is not None and got.task_id == 'g_task_0'

    co.abandon_task('g_task_0', 'worker_a')
    assert not co._lock.is_task_locked('g_task_0'), 'the claim was not released'
    assert led.get_task('g_task_0').status == TaskStatus.IN_PROGRESS

    # Not retried at once: an immediate re-claim would hammer the same
    # rate-limited endpoint every poll.
    assert co.claim_next_task('worker_b') is None

    # Once the claim is older than the orphan threshold, recovery re-queues it.
    old = datetime.now() - timedelta(seconds=tc._ORPHAN_AFTER_S + 60)
    led.get_task('g_task_0').context['claimed_at'] = old.isoformat()
    again = co.claim_next_task('worker_b')
    assert again is not None and again.task_id == 'g_task_0'
