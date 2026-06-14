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
    from threadlocal import thread_local_data
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
    from threadlocal import thread_local_data
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
    from threadlocal import thread_local_data
    thread_local_data.set_request_id('live-user-turn')
    with request_id_context('daemon_stale'):
        # On THIS (request) thread the thread-local wins.
        assert _get_request_id() == 'live-user-turn'
