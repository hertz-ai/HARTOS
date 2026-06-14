"""Foreground-request gate — lets background daemons hand the shared LLM/model
to a user-facing request that is being served RIGHT NOW.

The yield gate (``dispatch.should_yield_to_user``) already backs daemons off when
the user was active in the last 10 minutes, but the daemon's STARVATION OVERRIDE
force-runs a tick after ~120 s of yielding so idle-hour flywheel work isn't
stalled forever.  That override is correct when the user has stepped AWAY (the
governor is just self-throttling) — but wrong when the user is *actively* waiting
on a chat response: it steals the 4B draft model mid-turn and the reply times out.

This module is the finer signal: ``foreground_active()`` is True only while a
user request is in flight *this instant* (a /chat handler wraps its work in
``foreground_request()``).  The override and the gate consult it so background
work never grabs the model out from under a live turn — independent of the
coarser 10-minute "recently active" window.

Thread-safe, dependency-free, single source.  ``enter``/``exit`` are balanced by
the context manager; a stray exit can never drive the count negative.
"""
import contextlib
import threading

_lock = threading.Lock()
# Condition over the SAME lock so background callers can BLOCK until the
# foreground count drops to zero (wait_until_clear) and exit_foreground can
# wake them — no polling.
_cond = threading.Condition(_lock)
_count = 0

# ── Cancel registry ──────────────────────────────────────────────────────
# Background LLM calls that were started while NO foreground request was in
# flight register a 0-arg "terminate me" callable here (e.g. one that shuts down
# the call's socket).  When a foreground request arrives, enter_foreground()
# fires them once, so an in-flight background call releases the shared model slot
# immediately instead of holding it for its full timeout.  The foreground chat's
# OWN calls never register (they start with foreground already active), so they
# are never cancelled.
_cancel_lock = threading.Lock()
_cancellables = set()  # type: set

# Optional discriminator consulted by mark_view: a 0-arg predicate returning
# False for a background/daemon request, so it does NOT mark foreground (and so a
# genuine user turn — not the daemon's own /chat dispatch — owns the 0->1 edge
# that fires the abort).  Kept here (dependency-free) so the single mark_view
# source serves both the HARTOS /chat and the bundled Nunba chat_route; HARTOS
# registers a check that reads the inbound request_id + dispatch
# .is_genuine_user_request.  None => unregistered => every view marks foreground
# (back-compat).
_genuine_check = None


def register_cancellable(fn) -> None:
    """Register a 0-arg callable that terminates an in-flight BACKGROUND LLM call.
    Caller MUST unregister in a finally.  No-op-safe."""
    if fn is None:
        return
    with _cancel_lock:
        _cancellables.add(fn)


def unregister_cancellable(fn) -> None:
    with _cancel_lock:
        _cancellables.discard(fn)


def _fire_cancellables() -> None:
    with _cancel_lock:
        fns = list(_cancellables)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass  # best-effort: a failed terminate just means the call runs out


def enter_foreground() -> None:
    """Mark that a user-facing request started being served.  On the 0->1 edge,
    terminate any in-flight background LLM calls so the user gets the model now."""
    global _count
    with _lock:
        _count += 1
        first = _count == 1
    if first:
        _fire_cancellables()


def exit_foreground() -> None:
    """Mark that a user-facing request finished (floored at zero).  Wakes any
    background caller parked in ``wait_until_clear`` when the count reaches 0."""
    global _count
    with _cond:  # same underlying _lock
        if _count > 0:
            _count -= 1
            if _count == 0:
                _cond.notify_all()


def foreground_active() -> bool:
    """True iff at least one user-facing request is being served right now."""
    return _count > 0


def wait_until_clear(timeout: float) -> bool:
    """Block until no foreground request is in flight, or ``timeout`` seconds
    elapse.  Returns True if the foreground cleared, False on timeout.

    For a BACKGROUND (autonomous daemon) LLM caller that wants to YIELD the
    shared local model to a live user turn before issuing its own call — so it
    never piles onto llama-server while the user is waiting.  An already-clear
    foreground returns immediately.  A non-positive timeout is a non-blocking
    poll."""
    if timeout <= 0:
        return _count == 0
    with _cond:
        return _cond.wait_for(lambda: _count == 0, timeout=timeout)


def in_flight() -> int:
    """Current number of in-flight foreground requests (diagnostics)."""
    return _count


@contextlib.contextmanager
def foreground_request():
    """Wrap a user-facing request so background daemons yield the model to it::

        with foreground_request():
            ... serve the /chat turn ...

    Always balances enter/exit, even on exception."""
    enter_foreground()
    try:
        yield
    finally:
        exit_foreground()


def set_genuine_check(fn) -> None:
    """Register the 0-arg predicate consulted by ``mark_view``.

    Return ``False`` for a background/daemon request so it does NOT mark
    foreground; ``None`` unregisters (every view marks foreground — the original
    behaviour).  HARTOS registers one that reads the inbound request_id and
    applies ``dispatch.is_genuine_user_request`` — the SAME discriminator the
    outbound monkeypatch (``llm_outbound_logger._is_background_call``) already
    uses, so the /chat gate and the call patch finally agree."""
    global _genuine_check
    _genuine_check = fn


# Optional accessor to the ONE canonical daemon-yield gate
# (``dispatch.should_yield_to_user``).  ``core/`` background loops must NOT
# import ``integrations/`` (layering rule: integrations -> core OK, core ->
# integrations BANNED), so dispatch registers ITSELF here via ``set_yield_gate``
# (inversion of control, mirroring ``set_genuine_check``).  ``None`` => the gate
# is unregistered => fail-OPEN ``False`` so a core loop is never blocked on a
# missing/erroring gate (no regression vs. the pre-existing "no gate" behaviour).
_yield_gate = None


def set_yield_gate(fn) -> None:
    """Register the 0-arg predicate that backs ``should_yield_to_user``.

    ``dispatch.should_yield_to_user`` registers itself via this so ``core/``
    loops can consult the SINGLE canonical gate without importing
    ``integrations/``.  ``None`` unregisters (gate fails open)."""
    global _yield_gate
    _yield_gate = fn


def should_yield_to_user() -> bool:
    """Core-layer accessor to the ONE canonical daemon-yield gate.

    Returns whatever the registered gate (``dispatch.should_yield_to_user``,
    wired in via ``set_yield_gate``) reports.  Fail-OPEN ``False`` when no gate
    is registered or the gate raises — a core loop is never blocked on a missing
    gate, so this introduces no regression over the prior "no gate" behaviour."""
    fn = _yield_gate
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def mark_view(fn):
    """Decorator: mark a GENUINE user request handler as a foreground turn for
    its whole duration, so background daemons yield the shared model to it.

    SINGLE SOURCE for both chat entrypoints — the standalone HARTOS ``/chat``
    route AND the bundled Nunba ``chat_route`` (which shadows /chat on :5000)
    apply this same decorator, so there is one foreground rule, not a per-app
    copy.  Generic (wraps any callable) and dependency-free.

    If a genuine-check is registered (``set_genuine_check``) and reports the
    request is NOT genuine (the daemon's own ``daemon_*`` /chat dispatch), the
    view runs WITHOUT marking foreground — so the daemon never trips the abort
    edge meant for a live user, never yields to itself, and the user's real turn
    owns the 0->1 edge.  A missing/raising check fails OPEN (marks foreground) so
    a real user turn is never accidentally starved.
    """
    import functools

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        genuine = True
        chk = _genuine_check
        if chk is not None:
            try:
                genuine = chk()
            except Exception:
                genuine = True  # fail-open: never starve a real user turn
        if genuine:
            with foreground_request():
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)
    return _wrapped
