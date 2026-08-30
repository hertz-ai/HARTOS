"""A burst of A2UI events must not freeze the desktop.

THE BUG, reported and measured on .69 on 2026-08-30. The user clicked the wifi
icon and the UI hung. Measured DURING the hang, every layer below the browser was
idle:

    SILENT FREEZE warnings this boot : 0      (the compositor IS presenting)
    hart-comp pid 1702               : S, wchan=do_epoll_wait
    WebKitWebProcess pid 2392        : S, wchan=poll_schedule_timeout
    shell service pid 2131           : 127 threads, ALL state S. Zero R, zero D.
    TCP connections to :6800         : 8, against a 12-thread pool

Nothing was blocked anywhere, so the block was inside the WebKit main loop. The
mechanism was unbounded at both ends:

  producer  /api/notifications/stream collected EVERY component newer than the
            cursor, with no cap, and emitted them as one JSON array.
  consumer  evtSrc.onmessage did events.forEach(...) synchronously, and every
            branch is DOM work (showToast, hartInstallIcon, HartHome.compose,
            HartPalette.paint, renderAgentOverlay).

The trigger was federation backfill: a freshly reflashed box has an empty
database, so first peer contact pushed dozens of events inside one second.

The per-agent cap at agent_ui_update (5 components per agent) does NOT bound
this: each federating peer mints its own agent id, so the TOTAL is unbounded.

THE RULE THESE TESTS ENFORCE: bound the batch, drop nothing. The cursor advances
only past events actually sent, so the remainder is emitted on the next
iteration rather than lost.

Run:
  pytest tests/unit/test_sse_batch_bounded.py -v
"""

import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine import liquid_ui_service as L  # noqa: E402

SERVICE_SRC = os.path.join(REPO, "integrations", "agent_engine",
                           "liquid_ui_service.py")


@pytest.fixture(scope="module")
def rendered():
    """The shell page as actually served. The JS lives in an f-string, so only
    the RENDERED output proves what the browser receives."""
    svc = L.LiquidUIService.__new__(L.LiquidUIService)
    svc._data_dir = os.environ.get("TEMP", ".")
    svc.port = 6800
    svc.renderer = "webkit"
    svc.theme = "auto"
    svc.voice_enabled = True
    svc.haptic_enabled = False
    svc.context_refresh_ms = 2000
    svc.a2ui_enabled = True
    svc.model_bus_port = 6790
    svc.backend_port = 6777
    svc._model_available = False
    return svc.render_desktop_shell()


# ── the producer's bound ────────────────────────────────────────────────────

def test_the_batch_cap_exists_and_is_sane():
    assert isinstance(L._SSE_MAX_EVENTS_PER_MESSAGE, int)
    assert 1 < L._SSE_MAX_EVENTS_PER_MESSAGE <= 32, (
        "too small and ordinary traffic needs many round trips; too large and "
        "one message can block the main thread again")


def test_a_batch_is_capped():
    events = [{"_ts": i} for i in range(50)]
    batch, _ = L.sse_next_batch(events, cursor=0, now=999, limit=8)
    assert len(batch) == 8


def test_the_cursor_lands_on_the_last_event_actually_sent():
    """THE ANTI-DROP GUARANTEE. If the cursor jumped past the batch, every
    event beyond the cap would be silently lost."""
    events = [{"_ts": i} for i in range(1, 51)]
    batch, cursor = L.sse_next_batch(events, cursor=0, now=999, limit=8)
    assert cursor == batch[-1]["_ts"] == 8
    assert cursor != 999, "the cursor must not jump to now while events pend"


def test_events_are_ordered_before_slicing():
    """Without an order, 'the first N' is arbitrary and the remainder cannot be
    resumed from a timestamp cursor."""
    events = [{"_ts": 5}, {"_ts": 1}, {"_ts": 9}, {"_ts": 3}]
    batch, cursor = L.sse_next_batch(events, cursor=0, now=999, limit=2)
    assert [e["_ts"] for e in batch] == [1, 3]
    assert cursor == 3


def test_nothing_pending_moves_the_cursor_to_now():
    """The only case where jumping forward is safe: there is nothing to jump
    over, and the caller emits a heartbeat instead."""
    batch, cursor = L.sse_next_batch([], cursor=5, now=1234, limit=8)
    assert batch == []
    assert cursor == 1234


def test_a_malformed_timestamp_never_moves_the_cursor():
    """A missing/None _ts must not become the cursor: it would either rewind the
    stream (re-sending forever) or skip ahead (dropping events)."""
    for bad in ({"_ts": None}, {}, {"_ts": "nonsense"}):
        batch, cursor = L.sse_next_batch([bad], cursor=42, now=999, limit=8)
        assert batch, "the event should still be sent"
        assert cursor == 42, "the cursor must hold when _ts is unusable"


def test_a_burst_is_delivered_exactly_once_in_order():
    """Drive the real decision function the way the producer loop does, over a
    30-event burst, and assert nothing is lost or repeated."""
    store = [{"_ts": i, "id": i} for i in range(1, 31)]
    delivered, cursor, rounds = [], 0, 0
    while True:
        pending = [e for e in store if e["_ts"] > cursor]
        batch, cursor = L.sse_next_batch(pending, cursor, now=10_000, limit=8)
        if not batch:
            break
        delivered.extend(batch)
        rounds += 1
        assert rounds < 100, "the cursor failed to advance — infinite loop"

    assert [e["id"] for e in delivered] == list(range(1, 31)), (
        "every event must be delivered exactly once, in order")
    assert rounds == 4, "30 events at 8 per message should take 4 messages"


def test_a_single_event_still_arrives_in_one_message():
    """The common case must not regress into extra round trips."""
    batch, cursor = L.sse_next_batch([{"_ts": 7, "id": 1}], cursor=0, now=999)
    assert len(batch) == 1 and cursor == 7


def test_a_limit_below_one_still_makes_progress():
    """Defensive: a misconfigured limit must not stall the stream forever."""
    batch, cursor = L.sse_next_batch([{"_ts": 1}, {"_ts": 2}], 0, 999, limit=0)
    assert len(batch) == 1 and cursor == 1


def test_the_decision_is_pure():
    """No hidden state: the same inputs give the same answer, and the caller's
    list is not mutated (the producer reuses it on the next iteration)."""
    events = [{"_ts": 3}, {"_ts": 1}, {"_ts": 2}]
    before = list(events)
    a = L.sse_next_batch(events, 0, 999, limit=2)
    b = L.sse_next_batch(events, 0, 999, limit=2)
    assert a == b
    assert events == before, "sse_next_batch mutated its input"


def test_the_producer_uses_the_shared_decision_and_holds_the_lock():
    """The one structural assertion worth keeping: collect/decide/advance must
    sit inside the condition variable and the yield must sit outside it.

    Inside, because a push landing between the CV release and the cursor
    assignment would get a _ts below the new cursor and be skipped forever — a
    silent drop, which is the exact failure the batching exists to prevent.
    Outside, because holding the CV across a socket write would block every
    pusher behind one slow client. This is about lock scope, which no unit test
    of the pure function can see.
    """
    src = open(SERVICE_SRC, encoding="utf-8").read()
    body = src[src.index("def notification_stream"):]
    body = body[:body.index("@app.route('/health'")]

    assert body.count("sse_next_batch(") == 1, (
        "the route must use the shared decision function exactly once")
    with_cv = body.index("with self._ui_event_cv:")
    decide = body.index("sse_next_batch(")
    yield_data = body.index('yield f"data:')
    assert with_cv < decide < yield_data
    assert "yield" not in body[with_cv:decide], (
        "the generator must not yield while holding the condition variable")


# ── the consumer's yielding ─────────────────────────────────────────────────

def test_the_page_no_longer_applies_a_batch_synchronously(rendered):
    """THE BUG ITSELF. events.forEach(...) on the main thread is what froze the
    desktop."""
    assert not re.search(r"events\.forEach\(function\(ev\)", rendered), (
        "the synchronous per-message forEach is back")


def test_the_page_queues_and_drains_across_frames(rendered):
    for token in ("_evQueue", "_evDraining", "_drainEvents", "_applyEvent",
                  "EV_PER_FRAME"):
        assert token in rendered, "missing %s in the served page" % token
    assert "requestAnimationFrame(_drainEvents)" in rendered, (
        "the drain must be scheduled on the paint clock so input gets a turn")


def test_onmessage_only_enqueues(rendered):
    """The handler must parse and push, never apply."""
    start = rendered.index("evtSrc.onmessage")
    handler = rendered[start:rendered.index("evtSrc.onerror", start)]
    assert "_evQueue.push" in handler
    for dom_call in ("showToast(", "renderAgentOverlay(", "HartHome.compose"):
        assert dom_call not in handler, (
            "%s is called straight from onmessage - that is the synchronous "
            "path again" % dom_call)


def test_a_failing_event_does_not_abandon_the_queue(rendered):
    """One bad event must not strand the rest: the apply is wrapped per event
    inside the drain loop."""
    start = rendered.index("function _drainEvents")
    drain = rendered[start:rendered.index("function _applyEvent", start)]
    assert "try { _applyEvent(ev); } catch(err) {}" in drain


def test_the_drain_reschedules_until_empty(rendered):
    start = rendered.index("function _drainEvents")
    drain = rendered[start:rendered.index("function _applyEvent", start)]
    assert "if(_evQueue.length)" in drain
    assert "requestAnimationFrame(_drainEvents)" in drain
    assert "_evDraining = false" in drain, (
        "the drain must clear its latch when done or a later burst never starts")


def test_every_original_event_type_still_handled(rendered):
    """Moving the body into _applyEvent must not have dropped a branch."""
    start = rendered.index("function _applyEvent")
    body = rendered[start:rendered.index("evtSrc.onmessage", start)]
    for branch in ("'notification'", "'app_installed'", "'home'",
                   "'home_compose'"):
        assert branch in body, "lost the %s branch" % branch
    for call in ("showToast(", "hartInstallIcon", "HartHome.compose",
                 "HartPalette.paint", "renderAgentOverlay("):
        assert call in body, "lost the %s call" % call


def test_the_rendered_js_has_balanced_braces(rendered):
    """The JS is built inside a Python f-string, where a brace slip is silent
    until the browser refuses to parse the page."""
    start = rendered.index("const evtSrc = new EventSource")
    block = rendered[start:rendered.index("evtSrc.onerror", start)]
    depth = 0
    for ch in block:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, "brace depth went negative in the SSE block"
    assert depth == 0, "unbalanced braces in the SSE block"
    assert "{{" not in block and "}}" not in block, (
        "doubled f-string braces leaked into the served JavaScript")
