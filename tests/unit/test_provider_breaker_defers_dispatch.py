"""local_chat_dispatch defers when the provider breaker is open (#106b b).

Central's daemon runs goal turns through distributed_agent.worker_loop
._execute_task -> local_chat_dispatch, NOT dispatch_goal.  The provider
breaker's pre-flight consumer was only in dispatch_goal, so on central the
breaker OPENed (the httpx feed works) but the worker path kept calling the
refusing provider and burning 402s inside the OPEN window (measured on central
2026-09-15: breaker OPEN 05:19:52, worker 402s still at 05:22:32/05:22:53).

local_chat_dispatch is the ONE in-process /chat call every daemon path
(dispatch_goal, _dispatch_single_instruction, worker_loop) funnels through, so
the non-consuming state()==OPEN check belongs there: OPEN -> 'deferred' (the
worker re-queues, no burn), HALF_OPEN lets one turn through for the wire feed
to resolve.

    python -m pytest tests/unit/test_provider_breaker_defers_dispatch.py -q
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import dispatch  # noqa: E402
from core.circuit_breaker import llm_provider_breaker, CircuitState  # noqa: E402

HOST = 'prov.example.test'


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_provider_breaker.reset(HOST)
    yield
    llm_provider_breaker.reset(HOST)


def _open():
    for _ in range(5):
        llm_provider_breaker.record_failure(HOST)
    assert llm_provider_breaker.state(HOST) == CircuitState.OPEN


def _dispatch():
    """Call local_chat_dispatch with the provider host forced to HOST and every
    downstream side effect stubbed; return (status, text, inner_mock)."""
    inner = MagicMock(return_value={'text': 'a real answer'})
    sem = MagicMock()
    sem.acquire.return_value = True
    with patch.object(dispatch, '_in_process_chat', return_value=inner), \
            patch.object(dispatch, 'is_user_recently_active', return_value=False), \
            patch.object(dispatch, '_dispatch_provider_host', return_value=HOST), \
            patch.object(dispatch, '_local_llm_semaphore', sem), \
            patch.object(dispatch, '_notify_watchdog_llm_start', lambda: None), \
            patch.object(dispatch, '_notify_watchdog_llm_end', lambda: None):
        status, text = dispatch.local_chat_dispatch(
            'hello', 1, 2, daemon_id='d1')
    return status, text, inner


def test_open_provider_breaker_defers_without_calling_the_provider():
    _open()
    status, text, inner = _dispatch()
    assert (status, text) == ('deferred', None), (
        'an open provider breaker did not defer the turn')
    inner.assert_not_called()


def test_closed_provider_breaker_runs_the_turn_normally():
    # breaker CLOSED (reset by the fixture): no over-block, the turn runs.
    status, text, inner = _dispatch()
    assert status == 'ok'
    assert text == 'a real answer'
    inner.assert_called_once()
