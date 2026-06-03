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
_count = 0


def enter_foreground() -> None:
    """Mark that a user-facing request started being served."""
    global _count
    with _lock:
        _count += 1


def exit_foreground() -> None:
    """Mark that a user-facing request finished (floored at zero)."""
    global _count
    with _lock:
        if _count > 0:
            _count -= 1


def foreground_active() -> bool:
    """True iff at least one user-facing request is being served right now."""
    return _count > 0


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
