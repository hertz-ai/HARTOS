"""Foreground preemption of autonomous-background LLM calls (B1 abort-in-flight).

A user's chat must reclaim the single local llama slot the instant they hit
enter — even from an autonomous daemon goal that is mid-generation.  The
mechanism, all at the existing httpx send choke point + the foreground signal:

  * Every :8082 call carries its caller in the X-HARTOS-Request-ID header.
    Daemon dispatches use 'daemon_<goal_id>' (dispatch.py); user turns do not.
    ``_is_background_call`` applies the CANONICAL dispatch.is_genuine_user_request
    rule — no duplicated prefix logic.
  * Background calls run on a SEPARATE, closable httpx client
    (http_pool.get_bg_llm_http_client).  ``enter_foreground`` fires the cancel
    registry → closes that client → its in-flight sockets drop → llama aborts
    those generations.  The foreground shared client is never touched.
  * ``wait_until_clear`` lets a background call yield to a live turn before it
    even contends.

Behavioural — exercises the real signal, the real client lifecycle, and the
real discriminator with real httpx.Request objects.  No grep tests.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

httpx = pytest.importorskip('httpx')

_URL = 'http://127.0.0.1:8082/v1/chat/completions'


@pytest.fixture(autouse=True)
def _clean():
    """Reset all process-global state this feature touches, before and after."""
    from core import foreground, http_pool
    from hartos.threadlocal import thread_local_data

    def _reset():
        while foreground.in_flight() > 0:
            foreground.exit_foreground()
        http_pool.close_bg_llm_http_client()
        # Drop registrations + the once-flag so each test starts fresh.
        with foreground._cancel_lock:
            foreground._cancellables.clear()
        http_pool._bg_cancel_registered = False
        try:
            thread_local_data.set_request_id(None)
        except Exception:
            pass

    _reset()
    yield
    _reset()


# ── Discriminator: who called? (the X-HARTOS-Request-ID header) ──────────

def test_is_background_call_uses_daemon_request_id():
    from core.llm_outbound_logger import _is_background_call
    daemon = httpx.Request('POST', _URL,
                           headers={'X-HARTOS-Request-ID': 'daemon_goal_42'})
    user = httpx.Request('POST', _URL,
                         headers={'X-HARTOS-Request-ID': 'req_user_7'})
    # daemon_* → autonomous background → cancellable
    assert _is_background_call(daemon) is True
    # a real user turn → never background
    assert _is_background_call(user) is False


def test_is_background_call_treats_empty_id_as_background():
    """No request_id anywhere → BACKGROUND (abortable), delegating to the
    canonical is_genuine_user_request (empty→non-user) exactly like the inbound
    gate. A real /chat always carries an id, so an untagged llama call is daemon
    work whose 'daemon_' tag was lost crossing the autogen worker-thread boundary
    — classifying it foreground (the old bespoke `if not rid: return False`) is
    what left empty-rid daemon 4B calls un-preemptible (#162)."""
    from core.llm_outbound_logger import _is_background_call
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('')  # deterministic: no leftover from another test
    assert _is_background_call(httpx.Request('POST', _URL)) is True


def test_thread_local_request_id_is_the_fallback():
    """When the header is absent, the thread-local request_id still classifies
    the call (covers callers that don't reach _annotate_request)."""
    from core.llm_outbound_logger import _is_background_call
    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('daemon_goal_99')
    assert _is_background_call(httpx.Request('POST', _URL)) is True
    thread_local_data.set_request_id('user_turn_1')
    assert _is_background_call(httpx.Request('POST', _URL)) is False


# ── Background client lifecycle: separate, closable, rebuildable ─────────

def test_bg_client_is_separate_from_foreground_client():
    from core import http_pool
    fg = http_pool.get_llm_http_client()
    bg = http_pool.get_bg_llm_http_client()
    assert bg is not fg          # distinct connection pools
    assert not bg.is_closed


def test_foreground_arrival_closes_bg_client_but_not_foreground():
    """enter_foreground fires the cancel registry → the background client is
    closed (its in-flight generations abort) while the foreground client the
    user's own turn rides on is left fully intact."""
    from core import http_pool, foreground
    fg = http_pool.get_llm_http_client()
    bg = http_pool.get_bg_llm_http_client()   # registers the closer
    assert not bg.is_closed

    foreground.enter_foreground()
    try:
        assert bg.is_closed is True       # background aborted
        assert fg.is_closed is False      # user's turn untouched
    finally:
        foreground.exit_foreground()


def test_bg_client_rebuilds_fresh_after_close():
    from core import http_pool, foreground
    bg = http_pool.get_bg_llm_http_client()
    foreground.enter_foreground()
    foreground.exit_foreground()
    assert bg.is_closed
    bg2 = http_pool.get_bg_llm_http_client()
    assert bg2 is not bg and not bg2.is_closed


# ── Yield primitive ──────────────────────────────────────────────────────

def test_wait_until_clear_times_out_while_a_turn_is_in_flight():
    from core import foreground
    foreground.enter_foreground()
    try:
        assert foreground.wait_until_clear(0.05) is False
    finally:
        foreground.exit_foreground()
    # Once the turn has exited, a non-blocking poll reports clear immediately.
    assert foreground.wait_until_clear(0) is True


def test_cancellables_fire_only_on_the_zero_to_one_edge():
    from core import foreground
    fired = []
    fn = lambda: fired.append(1)
    foreground.register_cancellable(fn)
    try:
        foreground.enter_foreground()    # 0 -> 1 : fire
        foreground.enter_foreground()    # 1 -> 2 : no fire
        assert fired == [1]
        foreground.exit_foreground()
        foreground.exit_foreground()
        foreground.enter_foreground()    # 0 -> 1 again : fire
        assert fired == [1, 1]
    finally:
        foreground.unregister_cancellable(fn)
        while foreground.in_flight() > 0:
            foreground.exit_foreground()


# ── End-to-end selection: the wrapper routes by caller identity ──────────

def test_select_send_client_routes_background_to_bg_client():
    """A daemon call is routed to the closable background client; a user call
    stays on the caller's own client.  This is the routing decision the patched
    httpx send makes for every :8082 request."""
    from core import http_pool
    from core.llm_outbound_logger import _select_send_client

    class _Sentinel:
        is_closed = False

    own = _Sentinel()   # stand-in for the caller's (foreground/shared) client
    daemon_req = httpx.Request('POST', _URL,
                               headers={'X-HARTOS-Request-ID': 'daemon_g1'})
    user_req = httpx.Request('POST', _URL,
                             headers={'X-HARTOS-Request-ID': 'user_1'})

    # No foreground in flight → background call goes straight to the bg client.
    chosen_bg = _select_send_client(own, daemon_req)
    assert chosen_bg is http_pool.get_bg_llm_http_client()
    assert chosen_bg is not own

    # User turn is never rerouted.
    assert _select_send_client(own, user_req) is own
