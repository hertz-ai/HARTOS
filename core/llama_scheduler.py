"""Slot-aware priority admission controller for the single local llama-server.

THE PROBLEM (#162): the local llama-server serves only ``n_slots`` concurrent
requests (the ``--parallel`` value — 2 on the reference box).  A user "hi" and
the autonomous daemon both POST chat-completions to it, on multiple transports
(httpx via autogen/openai, raw ``requests`` via the draft/casual ``pooled_post``
path).  The server has NO notion of priority — it serves FIFO across its slots —
so a user turn arriving while both slots are busy just queues behind daemon work
and times out.  The old foreground-preempt fixed only the httpx path and only as
a blunt "abort ALL background calls" edge; it was neither slot-aware nor a real
queue.

THE CONTRACT this enforces, for EVERY llama call regardless of transport:

  * VISIBLE in-flight registry — each granted request is recorded with its
    ``request_id`` + ``kind`` ('user' | 'daemon'); ``inflight()`` lets any new
    arrival see exactly what holds the slots.
  * SLOT-AWARE — at most ``n_slots`` requests are in flight at once (the real
    server concurrency; settable via ``set_slots`` / ``HEVOLVE_LLAMA_SLOTS``).
  * PRIORITY BLOCKING QUEUE — a 'user' request outranks every 'daemon' request;
    callers BLOCK in ``acquire`` until a slot is granted (or timeout).
  * PREEMPTION — a 'user' arriving to a full server whose slots include a
    'daemon' CANCELS that daemon's in-flight call (its registered ``cancel_fn``
    — the closable bg httpx client / requests session) and takes the freed slot
    IMMEDIATELY, ahead of anything queued (highest precedence).  Another 'user'
    is never preempted; users are FIFO among themselves ("unless another user's
    request overrides it" → the earlier user already holds it).
  * FAIL-OPEN — any internal error degrades to "granted immediately"; a bug
    here can never deadlock the chat hot path.

Scope: this owns SLOT admission + preemption only.  Re-dispatching a PREEMPTED
daemon GOAL (retry it later, no backoff) is the daemon loop's job
(agent_daemon) — the scheduler just aborts the in-flight call and frees the
slot.  Token-keyed (not rid-keyed) so colliding/empty rids never alias a slot.
"""
from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import os
import threading

logger = logging.getLogger(__name__)

_PRI = {'user': 0, 'daemon': 1}
_DEFAULT_SLOTS = 2


def _resolve_slots(n) -> int:
    if n:
        try:
            return max(1, int(n))
        except Exception:
            pass
    try:
        env = os.environ.get('HEVOLVE_LLAMA_SLOTS')
        if env:
            return max(1, int(env))
    except Exception:
        pass
    return _DEFAULT_SLOTS


class _Req:
    """One admission request.  Token-keyed by ``seq`` (unique) so two daemon
    calls with the same (often empty) rid never collide on a slot."""
    __slots__ = ('rid', 'kind', 'pri', 'seq', 'cancel_fn',
                 'admitted', 'canceled', 'event')

    def __init__(self, rid, kind, seq, cancel_fn):
        self.rid = rid or ''
        self.kind = 'user' if kind == 'user' else 'daemon'
        self.pri = _PRI[self.kind]
        self.seq = seq
        self.cancel_fn = cancel_fn
        self.admitted = False
        self.canceled = False
        self.event = threading.Event()

    # heapq tiebreak when (pri, seq) compares equal is impossible (seq unique),
    # but define ordering defensively so a heap of _Req never raises.
    def __lt__(self, other):
        return self.seq < other.seq


class LlamaScheduler:
    def __init__(self, n_slots=None):
        self._n = _resolve_slots(n_slots)
        self._lock = threading.Lock()
        self._inflight: dict = {}     # seq -> _Req  (the VISIBLE registry)
        self._wait: list = []         # heap of (pri, seq, _Req)
        self._seq = itertools.count(1)

    # ── introspection / visibility ──────────────────────────────────────────
    @property
    def n_slots(self) -> int:
        return self._n

    def set_slots(self, n) -> None:
        """Update the concurrency cap to the real server ``--parallel`` value.
        Promotes waiters if the cap grew."""
        with self._lock:
            self._n = _resolve_slots(n)
            self._promote_locked()

    def inflight(self):
        """List of ``(rid, kind)`` currently holding a slot — what any new
        arrival can see in flight (the steward's 'a request shall identify
        itself as daemon or user' requirement)."""
        with self._lock:
            return [(r.rid, r.kind) for r in self._inflight.values()]

    def stats(self) -> dict:
        with self._lock:
            kinds = [r.kind for r in self._inflight.values()]
            return {
                'n_slots': self._n,
                'in_flight': len(self._inflight),
                'in_flight_kinds': kinds,
                'waiting': len(self._wait),
                'waiting_kinds': [r.kind for (_, _, r) in self._wait
                                  if not r.canceled],
            }

    # ── core admission ──────────────────────────────────────────────────────
    def acquire(self, rid, kind='daemon', cancel_fn=None, timeout=None):
        """Block until a slot is granted.  Returns an opaque token (pass to
        ``release``) on grant, or ``None`` on timeout.  A 'user' preempts an
        in-flight 'daemon' if the server is otherwise full."""
        try:
            req = _Req(rid, kind, next(self._seq), cancel_fn)
        except Exception:
            return None  # fail-open: caller proceeds unscheduled

        victim = None
        try:
            with self._lock:
                if len(self._inflight) < self._n:
                    self._admit_locked(req)
                elif req.kind == 'user':
                    victim = self._pick_daemon_locked()
                    if victim is not None:
                        self._evict_locked(victim)   # free the daemon's slot
                        self._admit_locked(req)       # user takes it NOW
                if not req.admitted:
                    heapq.heappush(self._wait, (req.pri, req.seq, req))
            # cancel the preempted daemon OUTSIDE the lock (close() may block on
            # a socket).  Its own release() later is a no-op (already evicted).
            if victim is not None and victim.cancel_fn:
                try:
                    victim.cancel_fn()
                    logger.info("llama_scheduler: user %s preempted daemon %s",
                                req.rid or '<empty>', victim.rid or '<empty>')
                except Exception:
                    pass
            if req.admitted:
                return req
            granted = req.event.wait(timeout)
            if granted and req.admitted:
                return req
            # timeout: lazy-delete from the heap
            with self._lock:
                req.canceled = True
            return None
        except Exception:
            # Never let a scheduler bug break the call — admit (untracked).
            return req

    def release(self, token) -> None:
        if token is None:
            return
        try:
            with self._lock:
                self._inflight.pop(getattr(token, 'seq', None), None)
                self._promote_locked()
        except Exception:
            pass

    @contextlib.contextmanager
    def slot(self, rid, kind='daemon', cancel_fn=None, timeout=None):
        """``with get_scheduler().slot(rid, kind, cancel_fn): resp = call()``.
        Yields the token (``None`` on timeout — caller still proceeds, fail-open).
        Always releases."""
        tok = self.acquire(rid, kind, cancel_fn, timeout)
        try:
            yield tok
        finally:
            self.release(tok)

    # ── helpers (lock held) ─────────────────────────────────────────────────
    def _admit_locked(self, req):
        req.admitted = True
        self._inflight[req.seq] = req

    def _evict_locked(self, victim):
        self._inflight.pop(victim.seq, None)
        victim.admitted = False

    def _pick_daemon_locked(self):
        # Oldest daemon first (lowest seq) — fairest victim choice.
        best = None
        for r in self._inflight.values():
            if r.kind == 'daemon' and (best is None or r.seq < best.seq):
                best = r
        return best

    def _promote_locked(self):
        while len(self._inflight) < self._n and self._wait:
            _, _, req = heapq.heappop(self._wait)
            if req.canceled or req.admitted:
                continue
            self._admit_locked(req)
            req.event.set()


# ── module singleton ─────────────────────────────────────────────────────────
_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> LlamaScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = LlamaScheduler()
    return _scheduler
