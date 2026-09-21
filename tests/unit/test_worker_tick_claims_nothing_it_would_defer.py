"""A worker tick that could only defer claims nothing.

Measured on the owner's desktop 2026-09-20 15:41-15:58 (installed build,
coordinator ledger of 9,531 tasks, 72 MB): four persisted tasks cycled
claim -> local_chat_dispatch "yielded to an active user" -> DEFERRED ->
undefer -> claim, 24/24/22 transitions in three minutes, every one a full
json.dump of the ledger, 18 writes a minute, ~1.2 GB/min, for as long as the
owner was using the machine.  The worker claimed first and asked afterwards.

Now it asks first, of the same gates the dispatcher answers with:
should_yield_to_user (the one gate every other daemon already consults), the
provider breaker local_chat_dispatch checks first, and the Nunba adapter's
readiness flag behind its 'hartos_loading' answer.  Behavioural: the real
coordinator, ledger and lock, the real worker tick, a spy on the backend's
save(); each closed gate leaves the task PENDING, the lock free and the ledger
unwritten, and the open gate still claims, runs and completes.

    python -m pytest tests/unit/test_worker_tick_claims_nothing_it_would_defer.py -q
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from core.circuit_breaker import llm_provider_breaker  # noqa: E402
from integrations.agent_engine import dispatch  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from integrations.distributed_agent.task_coordinator import (  # noqa: E402
    DistributedTaskCoordinator)
from integrations.distributed_agent.worker_loop import (  # noqa: E402
    DistributedWorkerLoop)

TASK = 'g_task_0'


@pytest.fixture
def world(tmp_path):
    led = SmartLedger('coord', 'gate', str(tmp_path), backend=InMemoryBackend())
    co = DistributedTaskCoordinator(ledger=led, task_lock=InMemoryTaskLock(),
                                    verifier=MagicMock(), baseline=MagicMock())
    co.submit_goal('obj', [{'task_id': TASK, 'description': 'd'}],
                   {'prompt': 'obj'}, goal_id='g')
    loop = DistributedWorkerLoop()
    loop._node_id = 'worker-under-test'
    return led, co, loop


def _tick(loop, co, led, **gates):
    """One tick with every gate pinned open unless a test closes one."""
    yield_ = gates.get('yield_', False)
    breaker_host = gates.get('breaker_host', '')
    llm_busy = gates.get('llm_busy', False)
    adapter = gates.get('adapter', None)     # None = absent (native HARTOS)
    patches = [
        patch.object(loop, '_get_coordinator', return_value=co),
        patch.object(dispatch, 'should_yield_to_user', return_value=yield_),
        patch.object(dispatch, 'get_last_yield_reason',
                     return_value='user_active' if yield_ else None),
        patch.object(dispatch, 'local_dispatch_provider_breaker_open',
                     return_value=breaker_host),
        patch.object(dispatch, 'local_dispatch_llm_busy',
                     return_value=llm_busy),
        patch.object(dispatch, 'local_chat_dispatch',
                     return_value=('ok', 'a real answer')),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda p: (True, '', p)),
        patch('security.hive_guardrails.GuardrailEnforcer.after_response',
              return_value=(True, '')),
        patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
              return_value=MagicMock()),
        patch.object(led.backend, 'save', wraps=led.backend.save),
    ]
    mods = {}
    if adapter is not None:
        stub = types.ModuleType('routes.hartos_backend_adapter')
        stub.is_hartos_initialized = lambda: adapter
        mods = {'routes.hartos_backend_adapter': stub}
    else:
        mods = {'routes.hartos_backend_adapter': None}
    with patch.dict(sys.modules, mods):
        ctx = [p.__enter__() for p in patches]
        try:
            loop._tick()
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
    return ctx[-1]   # the save spy


def _untouched(led, co):
    task = led.get_task(TASK)
    assert task.status == TaskStatus.PENDING
    assert 'claimed_by' not in task.context
    assert not co._lock.is_task_locked(TASK)


def test_a_user_holding_the_llm_means_no_claim_and_no_write(world):
    led, co, loop = world
    saves = _tick(loop, co, led, yield_=True)
    _untouched(led, co)
    assert saves.call_count == 0, 'the tick must not touch the ledger'


def test_an_open_provider_breaker_means_no_claim(world):
    led, co, loop = world
    saves = _tick(loop, co, led, breaker_host='api.example.test')
    _untouched(led, co)
    assert saves.call_count == 0


def test_a_busy_local_llm_means_no_claim(world):
    """The fourth condition, added 2026-09-21: the first three shipped and
    the churn carried on through this hole.  Measured on the installed build,
    08:54-09:02 IST: 18 claims and 18 deferrals in eight minutes, every one
    of them claiming a task, blocking the full five seconds of
    _local_llm_semaphore.acquire(timeout=5) and then deferring."""
    led, co, loop = world
    saves = _tick(loop, co, led, llm_busy=True)
    _untouched(led, co)
    assert saves.call_count == 0


def test_hartos_still_loading_means_no_claim(world):
    led, co, loop = world
    saves = _tick(loop, co, led, adapter=False)
    _untouched(led, co)
    assert saves.call_count == 0


def test_with_every_gate_open_the_tick_claims_runs_and_completes(world):
    led, co, loop = world
    saves = _tick(loop, co, led, adapter=True)
    task = led.get_task(TASK)
    assert task.status == TaskStatus.COMPLETED
    assert task.context['claimed_by'] == 'worker-under-test'
    assert saves.call_count == 2, 'one write for the claim, one for the completion'


def test_a_gate_that_cannot_be_read_never_wedges_the_worker(world):
    led, co, loop = world
    with patch.object(dispatch, 'should_yield_to_user',
                      side_effect=RuntimeError('signal unreadable')):
        assert DistributedWorkerLoop._dispatch_would_defer() in (
            None, 'hartos_loading')


class TestTheSlotCountNeverLeaks:
    """The hazard in counting slots beside a semaphore: if the count is ever
    raised without being lowered, local_dispatch_llm_busy() answers True
    forever, the gate above skips every tick, and the worker silently stops
    claiming anything.  That failure looks exactly like "the daemon went
    quiet" and would be brutal to find, so each way a turn can end has its
    own test.  The dispatch is driven for real; only the route it calls and
    the watchdog notify are stood in for.
    """

    @pytest.fixture(autouse=True)
    def _idle_to_start(self):
        assert dispatch._local_llm_inflight == 0, 'a prior test leaked a slot'
        yield
        dispatch._local_llm_inflight = 0

    @staticmethod
    def _run(route, notify=lambda: None):
        with patch.object(dispatch, '_in_process_chat', return_value=route), \
             patch.object(dispatch, 'is_user_recently_active',
                          return_value=False), \
             patch.object(dispatch, 'local_dispatch_provider_breaker_open',
                          return_value=''), \
             patch.object(dispatch, '_notify_watchdog_llm_start',
                          side_effect=notify), \
             patch.object(dispatch, '_notify_watchdog_llm_end',
                          side_effect=lambda: None):
            return dispatch.local_chat_dispatch('p', 'u', 'a',
                                                native_fallback=False)

    @staticmethod
    def _slot_is_free() -> bool:
        """The semaphore itself, not the count: they must agree."""
        if dispatch._local_llm_semaphore.acquire(blocking=False):
            dispatch._local_llm_semaphore.release()
            return True
        return False

    def test_a_turn_that_raises_gives_its_slot_back(self):
        def _boom(**kw):
            raise RuntimeError('the model died mid-turn')

        assert self._run(_boom) == ('unavailable', None)
        assert dispatch._local_llm_inflight == 0
        assert dispatch.local_dispatch_llm_busy() is False
        assert self._slot_is_free()

    def test_a_watchdog_notify_that_raises_gives_its_slot_back(self):
        """This one used to leak the semaphore itself: the notify sat between
        the acquire and the try, so a raise there took the node's only local
        LLM slot for the life of the process."""
        def _boom():
            raise RuntimeError('watchdog unavailable')

        assert self._run(lambda **kw: {'response': 'hi'}, notify=_boom) == (
            'unavailable', None)
        assert dispatch._local_llm_inflight == 0
        assert self._slot_is_free()

    def test_a_turn_that_succeeds_gives_its_slot_back(self):
        status, _ = self._run(lambda **kw: {'response': 'hi'})
        assert status == 'ok'
        assert dispatch._local_llm_inflight == 0
        assert self._slot_is_free()

    def test_the_count_is_raised_while_the_turn_is_in_flight(self):
        """Otherwise it would answer False during the very window it exists
        to describe."""
        seen = {}

        def _look(**kw):
            seen['inflight'] = dispatch._local_llm_inflight
            seen['busy'] = dispatch.local_dispatch_llm_busy()
            return {'response': 'hi'}

        self._run(_look)
        assert seen['inflight'] == 1
        assert seen['busy'] is True, (
            'a turn holding the only slot must read as busy')

    def test_the_count_never_goes_negative(self):
        dispatch._leave_local_llm_flight()
        assert dispatch._local_llm_inflight == 0, (
            'a stray release must not make a busy node look idle')

    def test_an_unreadable_count_reads_as_not_busy(self, monkeypatch):
        """Fail-open, like its three siblings: a capacity check may skip a
        tick, never wedge the worker."""
        monkeypatch.delattr(dispatch, '_LOCAL_LLM_MAX_CONCURRENT')
        assert dispatch.local_dispatch_llm_busy() is False


def test_the_breaker_accessor_is_the_dispatchers_own_check():
    """local_chat_dispatch and the worker read one breaker, one way."""
    host = 'provider.example.test'
    with patch.object(dispatch, '_dispatch_provider_host', return_value=host):
        llm_provider_breaker.reset(host)
        assert dispatch.local_dispatch_provider_breaker_open() == ''
        for _ in range(llm_provider_breaker._threshold):
            llm_provider_breaker.record_failure(host)
        try:
            assert dispatch.local_dispatch_provider_breaker_open() == host
            assert dispatch.local_chat_dispatch('p', 'u', 'a', native_fallback=False) \
                == ('deferred', None)
        finally:
            llm_provider_breaker.reset(host)
