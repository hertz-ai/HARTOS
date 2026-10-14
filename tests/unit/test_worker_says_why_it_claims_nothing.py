"""The distributed worker says why it claims nothing, once per change.

Measured on the owner's desktop 2026-10-10: the worker thread was alive
(py-spy) and had claimed nothing since 2026-10-05 18:48 (the coordinator
ledger, which every claim rewrites, was untouched), and the Nunba log held no
line from it at all in 5.5 hours.  Every reason it skips a tick was a DEBUG
line, and the bundled desktop logs at INFO.  Read from the ledger that day:
46 PENDING tasks, each demanding its own goal type as a capability this
worker does not advertise, every one of a goal already completed or paused.
Nothing in the log could have said so.

Behavioural: the real DistributedWorkerLoop._tick against a real
DistributedTaskCoordinator (in-memory ledger and lock).  Replaced: the gate
signals, the /chat call, the guardrails and the world-model bridge.

    python -m pytest tests/unit/test_worker_says_why_it_claims_nothing.py -q
"""
import contextlib
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from integrations.agent_engine import dispatch  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from integrations.distributed_agent.task_coordinator import (  # noqa: E402
    DistributedTaskCoordinator)
from integrations.distributed_agent.worker_loop import (  # noqa: E402
    DistributedWorkerLoop)
from tests.unit.module_swap import swap_modules  # noqa: E402

TASK = 'g_task_0'


@pytest.fixture
def world(tmp_path):
    led = SmartLedger('coord', 'idle', str(tmp_path), backend=InMemoryBackend())
    co = DistributedTaskCoordinator(ledger=led, task_lock=InMemoryTaskLock(),
                                    verifier=MagicMock(), baseline=MagicMock())
    # The live shape: the task demands a capability this worker lacks.
    co.submit_goal('obj', [{'task_id': TASK, 'description': 'd',
                            'capabilities': ['p2p_food']}],
                   {'prompt': 'obj'}, goal_id='g')
    loop = DistributedWorkerLoop()
    loop._node_id = 'worker-under-test'
    loop._capabilities = ['marketing', 'news']
    return led, co, loop


def _tick(loop, co, yield_=False):
    patches = [
        patch.object(loop, '_get_coordinator', return_value=co),
        patch.object(dispatch, 'should_yield_to_user', return_value=yield_),
        patch.object(dispatch, 'get_last_yield_reason',
                     return_value='user_present' if yield_ else None),
        patch.object(dispatch, 'local_dispatch_provider_breaker_open',
                     return_value=''),
        patch.object(dispatch, 'local_dispatch_llm_busy', return_value=False),
        patch.object(dispatch, 'local_chat_dispatch',
                     return_value=('ok', 'a real answer')),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda p: (True, '', p)),
        patch('security.hive_guardrails.GuardrailEnforcer.after_response',
              return_value=(True, '')),
        patch('integrations.agent_engine.world_model_bridge.'
              'get_world_model_bridge', return_value=MagicMock()),
        swap_modules({'routes.hartos_backend_adapter': None}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        loop._tick()


def _said(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.INFO
            and 'Distributed worker claiming' in r.getMessage()]


def test_an_unclaimable_queue_is_said_once_not_every_tick(world, caplog):
    """FAILS BEFORE THE FIX: three ticks, nothing claimed, no line at all."""
    led, co, loop = world
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        for _ in range(3):
            _tick(loop, co)
    assert led.get_task(TASK).status == TaskStatus.PENDING
    assert _said(caplog) == [
        "Distributed worker claiming nothing: no pending task its "
        "capabilities ['marketing', 'news'] can claim"], _said(caplog)


def test_each_change_of_reason_is_said_and_a_claim_ends_it(world, caplog):
    led, co, loop = world
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        _tick(loop, co)                      # nothing it can claim
        _tick(loop, co, yield_=True)         # the person is here
        _tick(loop, co, yield_=True)
        loop._capabilities = ['marketing', 'p2p_food']
        _tick(loop, co)                      # claims and runs it
    assert led.get_task(TASK).status == TaskStatus.COMPLETED
    said = _said(caplog)
    assert said[0].endswith("['marketing', 'news'] can claim"), said
    assert said[1] == ('Distributed worker claiming nothing: waiting '
                       '(user_present)'), said
    assert said[2].startswith('Distributed worker claiming again'), said
    assert len(said) == 3, said


def test_a_reason_the_worker_keeps_coming_back_to_is_said_once(world, caplog):
    """The first version of this said every change, and during a daemon's
    turn the gate opens and closes with each model call: measured on the
    owner's desktop 2026-10-10 11:04-11:09 IST, eight lines in five minutes
    alternating "local LLM busy" and the queue's own reason.  A reason said
    in the last ten minutes is not said again; after that it is."""
    import time as _time
    led, co, loop = world
    clock = [1000.0]
    with caplog.at_level(logging.INFO, logger='hevolve_social'), \
            patch.object(_time, 'monotonic', lambda: clock[0]):
        for _ in range(4):                  # busy, free, busy, free ...
            _tick(loop, co, yield_=True)
            clock[0] += 15
            _tick(loop, co)
            clock[0] += 15
        assert len(_said(caplog)) == 2, _said(caplog)
        clock[0] += loop._IDLE_RESAY_S      # ten minutes on, still flapping
        _tick(loop, co, yield_=True)
        _tick(loop, co)
    said = _said(caplog)
    assert len(said) == 4, said
    assert said[0] == said[2] == ('Distributed worker claiming nothing: '
                                  'waiting (user_present)'), said
    assert said[1] == said[3], said


def test_a_failing_tick_is_said_too(world, caplog):
    """A tick that raises was a DEBUG line; it is now said, once."""
    led, co, loop = world
    loop._interval = 0
    loop._running = True
    calls = []

    def _boom():
        calls.append(1)
        if len(calls) >= 2:
            loop._running = False
        raise RuntimeError('ledger unreadable')

    with caplog.at_level(logging.INFO, logger='hevolve_social'), \
            patch.object(loop, '_tick', side_effect=_boom), \
            patch('time.sleep'):
        loop._loop()
    assert _said(caplog) == [
        'Distributed worker claiming nothing: its tick failed: '
        'ledger unreadable'], _said(caplog)
