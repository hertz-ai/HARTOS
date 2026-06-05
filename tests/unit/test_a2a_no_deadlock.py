"""A2AContextExchange must not self-deadlock on a reentrant lock acquire (#82).

CI hang (>120s at internal_agent_communication.py:301): a delegation flow holds
self.lock and then calls send_message(), which for an unregistered recipient
calls register_agent() — re-acquiring the SAME lock. With a non-reentrant
threading.Lock that self-deadlocks the calling thread. The lock is now an RLock.

Behavioural tests on the REAL object:
  * reproduce the exact scenario (hold the lock, then send_message) in a worker
    thread and assert it finishes — old Lock hangs, RLock completes;
  * a direct invariant that the lock is reentrant.
No grep tests.
"""
from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.internal_comm.internal_agent_communication import A2AContextExchange

# Warm the lazy `from security.crypto import A2ACrypto` that send_message does,
# so the timed section below measures ONLY the lock path, not a cold import.
try:
    import security.crypto  # noqa: F401
except Exception:
    pass


def test_send_message_under_held_lock_does_not_deadlock():
    comm = A2AContextExchange(skill_registry=MagicMock())
    done = threading.Event()
    captured = {}

    def _run():
        try:
            # The CI scenario: a delegation method already holds self.lock, then
            # calls send_message() to an UNREGISTERED agent, which re-acquires
            # the lock via register_agent().
            with comm.lock:
                captured['mid'] = comm.send_message(
                    'agentA', 'unregistered_B', 'task', {'x': 1})
            done.set()
        except Exception as e:  # pragma: no cover - surfaced via assert below
            captured['err'] = repr(e)
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(timeout=10), (
        "send_message deadlocked while the caller held self.lock — "
        "register_agent re-acquires it; needs RLock, not Lock")
    assert 'err' not in captured, captured.get('err')
    assert captured.get('mid')
    assert len(comm.get_messages('unregistered_B')) == 1   # auto-registered + queued


def test_lock_is_reentrant():
    comm = A2AContextExchange(skill_registry=MagicMock())
    assert comm.lock.acquire(blocking=False)
    try:
        nested = comm.lock.acquire(blocking=False)   # RLock -> True; Lock -> False
        try:
            assert nested is True, "self.lock is not reentrant (would self-deadlock)"
        finally:
            if nested:
                comm.lock.release()
    finally:
        comm.lock.release()
