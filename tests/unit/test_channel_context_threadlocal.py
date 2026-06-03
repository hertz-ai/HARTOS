"""channel_context must be per-request (thread-local), not a shared-singleton attr.

The agent's channel tools read "where the current message came from" to route/
tailor a reply.  Two concurrent inbound messages (Discord-A, Telegram-B) have
different origins, so the value MUST be isolated per request/thread.  It used to
be assigned as a bare attribute on the module-level ``thread_local_data``
singleton (`thread_local_data.channel_context = …`), which is shared across all
threads — so under concurrency one request clobbered the other and the agent
could reply into the wrong channel.

Behavioural: real threads set different contexts and each must read back its own;
a fresh thread must see None (no leak from another request).  No grep tests.
"""
from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _clear():
    from threadlocal import thread_local_data
    thread_local_data.clear_channel_context()


def test_set_then_get_on_same_thread():
    from threadlocal import thread_local_data
    _clear()
    ctx = {'channel': 'discord', 'sender_id': 'u1', 'chat_id': 'c1'}
    thread_local_data.set_channel_context(ctx)
    assert thread_local_data.get_channel_context() == ctx
    thread_local_data.clear_channel_context()
    assert thread_local_data.get_channel_context() is None


def test_channel_context_is_isolated_across_concurrent_threads():
    """THE regression test: two threads set different channel contexts at the
    same time; each must read back its OWN.  A shared-singleton attribute fails
    this — the second setter's value would be seen by both."""
    from threadlocal import thread_local_data
    results = {}
    errors = []
    # Barrier(2): guarantee BOTH threads have set before EITHER reads, so a
    # shared store would have been clobbered by the time we read.
    barrier = threading.Barrier(2, timeout=5)

    def worker(name, ctx):
        try:
            thread_local_data.set_channel_context(ctx)
            barrier.wait()
            results[name] = thread_local_data.get_channel_context()
        except Exception as e:  # pragma: no cover - surfaces barrier timeout
            errors.append((name, e))

    a = {'channel': 'discord', 'chat_id': 'A'}
    b = {'channel': 'telegram', 'chat_id': 'B'}
    t1 = threading.Thread(target=worker, args=('a', a))
    t2 = threading.Thread(target=worker, args=('b', b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors, errors
    assert results['a'] == a   # discord thread never saw telegram
    assert results['b'] == b


def test_fresh_thread_sees_none_not_a_leaked_value():
    """A pooled worker thread that never set a channel must read None, not a
    prior request's context."""
    from threadlocal import thread_local_data
    # Pollute the MAIN thread's value; the child thread must not inherit it.
    thread_local_data.set_channel_context({'channel': 'slack'})
    out = {}

    def worker():
        out['v'] = thread_local_data.get_channel_context()

    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert out['v'] is None
    thread_local_data.clear_channel_context()


def test_agent_tools_reader_uses_the_accessor():
    """The channels tool helper reads via the thread-local accessor (the same
    value the /chat handler set), so the agent's send/get_channel_context tools
    see the live per-request origin."""
    from threadlocal import thread_local_data
    from integrations.channels.agent_tools import _get_channel_context
    _clear()
    assert _get_channel_context() is None
    ctx = {'channel': 'matrix', 'sender_name': 'Ann'}
    thread_local_data.set_channel_context(ctx)
    assert _get_channel_context() == ctx
    thread_local_data.clear_channel_context()
