"""Daemon request_id must survive the autogen worker-thread/context boundary
so the foreground-preempt can identify — and yield/abort — background LLM calls.

Root cause (2026-06-14, llm_outbound.jsonl):
  ``threadlocal.ThreadLocalData`` stores request_id in a ``threading.local()``,
  which does NOT propagate into the worker thread/context where autogen issues
  its httpx send.  So for ~94% of daemon ``autogen.create``/``autogen.reuse``
  calls the outbound monkeypatch saw ``request_id=''`` →
  ``is_genuine_user_request('')`` is True → the call was never routed to the
  closable background client (``core.http_pool.get_bg_llm_http_client``) → it
  never yielded to, nor was abortable by, a live user "hi".  On the single-slot
  llama-server that means one in-flight 64-600s daemon call fully blocks the
  user's turn.

The fix mirrors the WORKING ``source`` contextvar (``llm_outbound_logger
._source_var``), which DOES reach the send: a ``request_id`` contextvar bound by
``recipe``/``chat_agent`` via ``with_llm_context``, read as a fallback by
``_get_request_id()``.

These are behavioural tests: they bind the id through the real decorator and
read it back through the real ``_get_request_id`` from a context copied exactly
the way autogen's send inherits it — NO grep/source-shape assertions.
"""
import contextvars
import threading

import pytest

from core.llm_outbound_logger import (
    with_llm_context,
    request_id_context,
    _get_request_id,
)


@pytest.fixture(autouse=True)
def _clean_threadlocal_request_id():
    """``_get_request_id`` checks the thread-local FIRST; clear it around every
    test so a stale value from another test can't shadow the contextvar path."""
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id(None)
    yield
    thread_local_data.set_request_id(None)


def _read_request_id_in_copied_worker_context():
    """Mimic autogen's send: run ``_get_request_id`` on a fresh worker THREAD
    that inherited the caller's contextvars via ``copy_context()`` — exactly how
    the ``source`` label already reaches the send today.  A ``threading.local()``
    value is invisible across this boundary; a contextvar value is."""
    captured = {}
    ctx = contextvars.copy_context()
    t = threading.Thread(
        target=lambda: ctx.run(
            lambda: captured.__setitem__('rid', _get_request_id())))
    t.start()
    t.join()
    return captured.get('rid')


def test_daemon_request_id_propagates_to_copied_worker_context():
    """The bug fix: a daemon turn's id survives into the send context, so the
    outbound hook can classify it background and route it to the bg client."""
    seen = {}

    @with_llm_context('autogen.create')
    def fake_recipe(user_id, text, prompt_id, file_id, request_id):
        # The real send happens deep inside get_response_group -> autogen ->
        # httpx, on a context copied from here.  Simulate that read point.
        seen['rid'] = _read_request_id_in_copied_worker_context()
        return 'ok'

    fake_recipe('u', 'hi', 'p', None, 'daemon_42')
    assert seen['rid'] == 'daemon_42'
    # The downstream discriminator must now see it as background.
    assert seen['rid'].startswith('daemon_')


def test_user_request_id_propagates_and_is_not_daemon():
    """A genuine user turn that escalates into chat_agent keeps the user id
    (so it is correctly treated as foreground — never yielded/aborted)."""
    seen = {}

    @with_llm_context('autogen.reuse')
    def fake_chat_agent(user_id, text, prompt_id, file_id, request_id):
        seen['rid'] = _read_request_id_in_copied_worker_context()
        return 'ok'

    fake_chat_agent('u', 'hi', 'p', None, 'user-uuid-abc')
    assert seen['rid'] == 'user-uuid-abc'
    assert not seen['rid'].startswith('daemon_')


def test_with_llm_context_binds_request_id_passed_by_keyword():
    """Binding is BY NAME, so it works whether the call site is positional or
    keyword (recipe is called both ways across the codebase)."""
    seen = {}

    @with_llm_context('autogen.create')
    def fake_recipe(user_id, text, prompt_id, file_id, request_id):
        seen['rid'] = _read_request_id_in_copied_worker_context()
        return 'ok'

    fake_recipe('u', 'hi', 'p', file_id=None, request_id='daemon_99')
    assert seen['rid'] == 'daemon_99'


def test_threadlocal_request_id_does_not_cross_context():
    """Regression guard documenting WHY the contextvar is required: the
    thread-local set by the /chat handler does NOT reach a copied worker
    context — the exact gap that lost the daemon tag."""
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('daemon_should_not_leak')
    # No contextvar bound -> the worker context falls through to ''.
    assert (_read_request_id_in_copied_worker_context() or '') == ''


def test_request_id_context_resets_on_exit():
    """No leak across reused worker threads: the id is gone after the block."""
    with request_id_context('daemon_1'):
        assert _get_request_id() == 'daemon_1'
    assert (_get_request_id() or '') == ''


def test_threadlocal_still_takes_precedence_for_genuine_chat_turn():
    """The thread-local stays authoritative on the request thread, so a real
    /chat turn's id is never shadowed by a stale contextvar fallback."""
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('live-user-turn')
    with request_id_context('daemon_stale'):
        # On THIS (request) thread the thread-local wins.
        assert _get_request_id() == 'live-user-turn'


# ── Cross-contamination / end-to-end separation guards ───────────────────────
# The catastrophic failure is NOT the bug we fixed (daemon never yielding) but
# its inverse: a USER turn tagged daemon_* would be yielded/aborted — the user's
# own "hi" cancelling itself.  These prove the daemon and user identities never
# bleed into one another, even when they share an autogen worker thread/pool.

def test_no_cross_contamination_sequential_daemon_user_daemon():
    """Sequential decorated calls (daemon, then user, then daemon) each read
    back exactly their own id — no bleed through the shared contextvar."""
    seen = []

    @with_llm_context('autogen.create')
    def call(user_id, text, prompt_id, file_id, request_id):
        seen.append(_read_request_id_in_copied_worker_context())
        return 'ok'

    call('u1', 'hi', 'p', None, 'daemon_A')
    call('u2', 'hi', 'p', None, 'user-B-uuid')
    call('u3', 'hi', 'p', None, 'daemon_C')
    assert seen == ['daemon_A', 'user-B-uuid', 'daemon_C']


def test_persistent_reused_worker_does_not_bleed_daemon_into_user():
    """Worst case: a single-worker pool (forced thread reuse, exactly how
    autogen reuses send threads) runs a daemon call's context then a user
    call's context.  Because each call copies its OWN context — the way the
    ``source`` label already does — the reused worker never bleeds the daemon
    id into the user read."""
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)  # one thread, reused across calls
    try:
        reads = []

        @with_llm_context('autogen.create')
        def call(user_id, text, prompt_id, file_id, request_id):
            ctx = contextvars.copy_context()
            reads.append(pool.submit(lambda: ctx.run(_get_request_id)).result())
            return 'ok'

        call('u1', 'hi', 'p', None, 'daemon_A')
        call('u2', 'hi', 'p', None, 'user-B-uuid')
        assert reads == ['daemon_A', 'user-B-uuid']
    finally:
        pool.shutdown(wait=True)


def test_nested_user_call_inside_daemon_restores_daemon_on_exit():
    """Defensive (nesting shouldn't occur, but must be leak-proof): a user call
    nested inside a daemon scope reads the user id, and the daemon id is fully
    restored after the inner call returns."""
    @with_llm_context('autogen.reuse')
    def inner(user_id, text, prompt_id, file_id, request_id):
        return _get_request_id()

    @with_llm_context('autogen.create')
    def outer(user_id, text, prompt_id, file_id, request_id):
        before = _get_request_id()
        nested = inner('u', 't', 'p', None, 'user-INNER')
        after = _get_request_id()
        return before, nested, after

    before, nested, after = outer('u', 't', 'p', None, 'daemon_OUTER')
    assert before == 'daemon_OUTER'
    assert nested == 'user-INNER'
    assert after == 'daemon_OUTER'  # inner reset restored the outer id
