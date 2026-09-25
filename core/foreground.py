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

CROSS-PROCESS.  hart-agent-daemon.service is its own process (S1 finding,
2026-09-24), so there ``_count`` and dispatch's ``_last_user_chat_at`` are the
DAEMON'S counters: the backend serving the chat lives in another process and
neither counter can ever fire in the daemon, which left only model_pressure,
governor_throttle and the governor's mode gating it.  The edges of the count
therefore also hold a marker file (see "Cross-process markers" below), the
same transport the governor already reads to see the person at the desk, and
``foreground_active`` falls back to it when the in-process count says nothing.
"""
import contextlib
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Condition over the SAME lock so background callers can BLOCK until the
# foreground count drops to zero (wait_until_clear) and exit_foreground can
# wake them — no polling.
_cond = threading.Condition(_lock)
_count = 0

# ── Cross-process markers ────────────────────────────────────────────────
# One marker file per signal, its mtime the fact, exactly the idiom
# core.resource_governor._get_idle_ms_linux uses on the compositor's
# /run/hart/session/input-alive: no new transport, no socket, one os.stat.
#
#   foreground-active.<pid>  held while this process has a user request in
#                            flight: created on the count's 0->1 edge,
#                            removed on the 1->0 edge (the last exit).
#   user-chat.<pid>          touched by dispatch.mark_user_chat_activity on
#                            every GENUINE user chat.
#
# The two readers (foreground_active here, dispatch.is_user_recently_active)
# consult the markers ONLY when their in-process state says nothing (count 0,
# timestamp never set), so in the backend nothing changes and in the daemon
# the backend's turn becomes visible.
#
# Per-pid names rather than one shared file: two processes apply mark_view on
# a HART OS box (hart-backend on :6777 and the bundled Nunba on :5000, both
# User=hart), and with one file the first to finish would erase the other's
# live turn.  A reader takes the youngest LIVE FOREIGN marker: its own pid's
# marker is its own in-process state (which just said nothing); a writer that
# no longer exists left a crash leftover, which releases the daemon at once;
# and a marker older than the age bound is a leftover whose pid was reused,
# or a hung turn, which is what stops a crash from pinning the daemon forever.
_MARKER_DIR_ENV = 'HART_SESSION_MARKER_DIR'
_SESSION_RUN_DIR = '/run/hart/session'
FOREGROUND_MARKER = 'foreground-active'
USER_CHAT_MARKER = 'user-chat'
# A foreground marker older than this reads as over.  The same 10 minute
# window as dispatch._USER_CHAT_COOLDOWN: past it the coarser gate has let go
# of the same turn too, so a longer turn is not silently protected by one
# signal and not the other.  A turn that really runs longer refreshes nothing
# (no heartbeat thread); that is a recorded limit, not an oversight.
FOREGROUND_MARKER_MAX_AGE_S = 600.0
_marker_lock = threading.Lock()
_marker_warned = set()  # marker names whose write failure was logged once


def session_marker_dir() -> Optional[str]:
    """The ONE directory the session markers live in, or None when this
    deployment has no shared marker dir (a plain dev checkout, a CI
    container): the readers then have only their in-process state, exactly
    as before, and the tests of that state stay hermetic.

    Resolution, first hit wins:
      1. ``HART_SESSION_MARKER_DIR`` (tests; a supervisor that relocates the
         run dir), the same env-override contract as HART_INPUT_ALIVE_MARKER
         on the governor's reader and HART_INPUT_ALIVE_FLAG on its writer.
      2. ``/run/hart/session``, when it is a directory: the 0770 hart:hart
         tmpfs dir hart-session-supervisor.nix declares, where the
         compositor's input-alive marker already lives.  tmpfs, so a crash
         leftover cannot survive a reboot.
      3. ``<data root>/session`` on the bundled desktop (Nunba on Windows
         and macOS, core.config_cache.is_bundled), the data root being
         core.platform_paths.get_data_dir, which is where HARTOS_DATA_DIR
         and NUNBA_DATA_DIR are resolved.  Bundled means NUNBA_BUNDLED or
         sys.frozen, deliberately NOT "a data dir env is set": the deepbox
         test container sets NUNBA_DATA_DIR and several agents run tests in
         it concurrently, which a shared marker dir would cross-pollute.
    """
    override = os.environ.get(_MARKER_DIR_ENV, '').strip()
    if override:
        return override
    if os.path.isdir(_SESSION_RUN_DIR):
        return _SESSION_RUN_DIR
    try:
        from core.config_cache import is_bundled
        if is_bundled():
            from core.platform_paths import get_data_dir
            return os.path.join(get_data_dir(), 'session')
    except Exception:
        pass
    return None


def _own_marker_path(name: str) -> Optional[str]:
    d = session_marker_dir()
    if not d:
        return None
    return os.path.join(d, f'{name}.{os.getpid()}')


def touch_marker(name: str) -> bool:
    """Create or re-stamp this process's marker for ``name``.  Best-effort:
    returns False (and logs ONCE per name, at WARNING) when there is no
    marker dir or it is not writable, because a silent failure here is a
    deploy gap nobody would see: a unit under ProtectSystem=strict without
    /run/hart/session in ReadWritePaths writes nothing and the daemon keeps
    running inference through the person's turns."""
    p = _own_marker_path(name)
    if p is None:
        return False
    try:
        d = os.path.dirname(p)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(p, 'a'):
            pass
        os.utime(p, None)
        return True
    except OSError as e:
        if name not in _marker_warned:
            _marker_warned.add(name)
            logger.warning(
                "session marker %s not writable (%s): other processes "
                "(the agent daemon) cannot see this process's %s signal",
                p, e, name)
        return False


def clear_marker(name: str) -> None:
    """Remove this process's marker for ``name``.  Missing is fine."""
    p = _own_marker_path(name)
    if p is None:
        return
    try:
        os.remove(p)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Whether the process that wrote a marker still exists.  Unknown reads
    as alive: the safe direction is to keep yielding until the age bound."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == 'nt':
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
            if handle:
                k32.CloseHandle(handle)
                return True
            return k32.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def marker_age_s(name: str, max_age_s: float,
                 require_live: bool = True) -> Optional[float]:
    """Age in seconds of the youngest marker for ``name`` written by ANOTHER
    process and younger than ``max_age_s``; None when there is no such
    marker (or no marker dir).

    ``require_live`` (the default) also demands that the writer still
    exists: a foreground marker is a claim that a turn is in flight in that
    process, which a dead process cannot make, so a crash leftover releases
    the daemon at once.  The user-chat reader passes False: "the person
    chatted three minutes ago" is a fact about the person, still true after
    the backend that served it restarted, and only the window ends it.
    Age is checked before liveness so the costlier probe only runs for a
    marker that could count.  The mtime is a wall clock stamp compared
    against time.time(); a marker from the future (a clock step) clamps to
    age 0, which reads as live, the same rule as the governor's reader."""
    d = session_marker_dir()
    if not d:
        return None
    prefix = name + '.'
    me = os.getpid()
    now = time.time()
    best = None
    try:
        with os.scandir(d) as it:
            for entry in it:
                if not entry.name.startswith(prefix):
                    continue
                try:
                    pid = int(entry.name[len(prefix):])
                except ValueError:
                    continue
                if pid == me:
                    continue
                try:
                    age = max(0.0, now - entry.stat().st_mtime)
                except OSError:
                    continue
                if age >= max_age_s:
                    continue
                if best is not None and age >= best:
                    continue
                if require_live and not _pid_alive(pid):
                    continue
                best = age
    except OSError:
        return None
    return best


def _sync_foreground_marker() -> None:
    """Make the on-disk marker match the count AS IT IS NOW.  Called after
    each edge, under the marker lock and re-reading the count, so two edges
    racing on two threads (a 0->1 whose file write is delayed past another
    thread's 1->0) cannot leave a marker with nobody in flight or none with
    someone in flight: whichever sync runs last sees the final count."""
    with _marker_lock:
        if _count > 0:
            touch_marker(FOREGROUND_MARKER)
        else:
            clear_marker(FOREGROUND_MARKER)

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


# Monotonic timestamp of the last background-call abort (a "preempt").  Lets the
# daemon (dispatch.is_transient_deferral) recognise a goal whose LLM call was
# just aborted for a live user turn as a TRANSIENT defer — re-queue it next tick,
# never count it toward the 5-strike auto-pause ("queue the canceled daemon
# alone").  Initialised to -inf so it never reads "recent" before a real preempt.
_last_preempt_at = float('-inf')


def note_preempt() -> None:
    """Record that a background LLM call was just aborted for a foreground turn.
    Called by ``_fire_cancellables`` AND by the llama scheduler's per-slot
    preempt, so both preemption paths feed the daemon's transient-defer signal."""
    global _last_preempt_at
    try:
        _last_preempt_at = time.monotonic()
    except Exception:
        pass


def preempted_recently(window: float = 30.0) -> bool:
    """True if a foreground preempt fired within ``window`` seconds — the signal
    that lets a preempted daemon goal re-queue instead of backing off."""
    try:
        return (time.monotonic() - _last_preempt_at) < window
    except Exception:
        return False


def _fire_cancellables() -> None:
    note_preempt()
    with _cancel_lock:
        fns = list(_cancellables)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass  # best-effort: a failed terminate just means the call runs out


def enter_foreground() -> None:
    """Mark that a user-facing request started being served.  On the 0->1 edge,
    terminate any in-flight background LLM calls so the user gets the model now,
    and flip the ResourceGovernor to ACTIVE so the autonomous daemon swarm backs
    off immediately."""
    global _count
    with _lock:
        _count += 1
        first = _count == 1
    if first:
        _fire_cancellables()
        # The cross-process copy of this edge (see "Cross-process markers").
        _sync_foreground_marker()
        # Direct "a user turn just started" signal to the ONE canonical governor
        # mode-flip (resource_governor.report_user_activity).  A GENUINE user turn
        # owns this 0->1 edge (mark_view's _genuine_check keeps the daemon's own
        # /chat dispatch out), so the daemon swarm throttles NOW instead of waiting
        # for the governor's periodic CPU-attribution poll.  core->core import is
        # layering-legal; best-effort + fail-open so a missing/raising governor
        # never breaks the foreground gate.
        try:
            from core.resource_governor import get_governor
            get_governor().report_user_activity()
        except Exception:
            pass


def exit_foreground() -> None:
    """Mark that a user-facing request finished (floored at zero).  Wakes any
    background caller parked in ``wait_until_clear`` when the count reaches 0."""
    global _count
    last = False
    with _cond:  # same underlying _lock
        if _count > 0:
            _count -= 1
            if _count == 0:
                last = True
                _cond.notify_all()
    if last:
        _sync_foreground_marker()


def foreground_active() -> bool:
    """True iff at least one user-facing request is being served right now,
    in THIS process (the count) or, when the count says nothing, in another
    process on this machine that holds a live foreground marker younger
    than FOREGROUND_MARKER_MAX_AGE_S.  ``in_flight`` and ``wait_until_clear``
    stay in-process: they serve the backend's own model scheduling."""
    if _count > 0:
        return True
    return marker_age_s(FOREGROUND_MARKER, FOREGROUND_MARKER_MAX_AGE_S) is not None


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
