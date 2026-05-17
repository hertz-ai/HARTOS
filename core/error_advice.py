"""core/error_advice.py — central error advice (AOP-style decorator).

Single chokepoint for "something failed inside this function — what now?".
Spring's `@ExceptionAdvice` analogue: wrap a function (or use the
context-manager form), catch any exception, dispatch to:

  1. Structured logging  (always)
  2. Sentry capture       (if crash_reporter initialized — opt-in,
                           Nunba side; HARTOS server may also call init)
  3. Agent remediation    (opt-in via agent_remediation=True; creates
                           an AgentGoal that an autogen agent picks up
                           via the existing goal-seeding pipeline)

Then re-raises by default so callers still see the failure — the
advice is *additive* observation + remediation, not a try/except
black hole.

Why this lives in HARTOS / core: it composes the central agent goal
machinery (integrations.agent_engine.goal_manager.GoalManager) plus
the central crash reporter; both subsystems are HARTOS-owned, so
the decorator that fans out to them is too.  Nunba imports the
decorator the same way it imports any other shared utility (HARTOS
is bundled into the Nunba freeze via _deps/HARTOS).

SRP: this module does ONE thing — fan an exception out to the
central observability + remediation channels.  No retry logic
(callers own that), no recovery heuristics (deterministic recovery
sits *inside* the wrapped function, e.g. tts.package_installer.
_self_heal_missing_transitives), no policy decisions (severity +
remediation flag are caller-supplied).
"""

from __future__ import annotations

import functools
import logging
import threading
import traceback
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger('hevolve_error_advice')

# In-process throttle keyed by (category, error-fingerprint).  Stops a
# loop that hits the same exception 10× per second from creating 10
# agent goals + 10 Sentry events.  TTL is generous — same exception
# 5 minutes apart still counts as a fresh signal.
_THROTTLE_LOCK = threading.Lock()
_THROTTLE: dict[tuple[str, str], float] = {}
_THROTTLE_TTL_SEC = 300.0


def _fingerprint(exc: BaseException) -> str:
    """Stable error fingerprint for throttling.  Type + truncated msg.
    Intentionally NOT including traceback — same logical error from
    different call sites should still throttle as one."""
    return f"{type(exc).__name__}:{str(exc)[:120]}"


def _should_emit(category: str, exc: BaseException) -> bool:
    """True iff this (category, error) hasn't been emitted recently."""
    import time
    key = (category, _fingerprint(exc))
    now = time.monotonic()
    with _THROTTLE_LOCK:
        last = _THROTTLE.get(key)
        if last is not None and (now - last) < _THROTTLE_TTL_SEC:
            return False
        _THROTTLE[key] = now
        # Drop very old entries so the dict doesn't grow unbounded
        cutoff = now - (_THROTTLE_TTL_SEC * 4)
        for k, t in list(_THROTTLE.items()):
            if t < cutoff:
                del _THROTTLE[k]
    return True


def _try_sentry_capture(exc: BaseException, category: str, context: dict) -> None:
    """Best-effort Sentry capture via the Nunba crash_reporter helper.
    No-op when crash_reporter isn't importable (HARTOS server-only
    deploys, dev mode without Sentry SDK)."""
    try:
        from desktop.crash_reporter import capture_exception  # type: ignore
        capture_exception(exc, category=category, **context)
    except Exception:
        # Never let observability break the actual failure path
        pass


def _try_agent_remediation(
    category: str, exc: BaseException, context: dict, severity: str,
) -> None:
    """Best-effort: create an AgentGoal so an autogen agent can pick
    up the failure and try further remediation beyond the deterministic
    recovery the wrapped function may have already tried.

    Uses the canonical GoalManager.create_goal pattern from
    integrations.agent_engine.goal_seeding.auto_remediate_loopholes.
    Throttled per (category, error-fingerprint) so the same failure
    looping doesn't spawn N goals — one goal per failure shape per
    5-minute window is enough for an agent to investigate.

    No-op when GoalManager / DB session isn't reachable (Nunba dev
    mode without a HARTOS DB; HARTOS server before init_db)."""
    try:
        from integrations.agent_engine.goal_manager import GoalManager  # type: ignore
        from integrations.social.models import db_session  # type: ignore
    except Exception:
        return

    try:
        # goal_type='self_heal' is the registered builder
        # (goal_manager.py:1011 — `register_goal_type('self_heal',
        # _build_self_heal_prompt, tool_tags=['coding'])`).  Routes
        # to the local coding agent (Aider native backend) which has
        # tools to read source, write minimal fixes, run tests, and
        # iterate.  The config keys below must match what
        # _build_self_heal_prompt reads at goal_manager.py:836-856 —
        # exc_type / source_module / source_function /
        # occurrence_count / sample_traceback — otherwise the
        # coding agent gets blank fields in its prompt.
        tb_obj = getattr(exc, '__traceback__', None)
        sample_tb = (
            ''.join(traceback.format_tb(tb_obj)[-20:]) if tb_obj else ''
        )
        # Pull source module/function from the deepest frame for
        # deterministic identification of the failing site.
        source_module = ''
        source_function = ''
        if tb_obj is not None:
            last_frame = traceback.extract_tb(tb_obj)[-1] if tb_obj else None
            if last_frame is not None:
                source_module = last_frame.filename or ''
                source_function = last_frame.name or ''

        with db_session() as db:
            GoalManager.create_goal(
                db,
                goal_type='self_heal',
                title=f"Self-heal: {category} ({type(exc).__name__})",
                description=(
                    f"A {severity}-severity {category} failure escaped the "
                    f"deterministic recovery loop.  Investigate and remediate.\n\n"
                    f"Error: {type(exc).__name__}: {exc}\n\n"
                    f"Context: {context}\n\n"
                    f"Last 20 frames:\n{sample_tb}"
                ),
                config={
                    # Keys read by _build_self_heal_prompt — DO NOT rename
                    'exc_type': type(exc).__name__,
                    'source_module': source_module,
                    'source_function': source_function,
                    'occurrence_count': 1,  # throttle dedupes; this is
                                            # the count for THIS goal
                    'sample_traceback': sample_tb,
                    # Additional context for downstream consumers /
                    # operator review — not read by the prompt builder
                    # but stored on the goal for debugging.
                    'category': category,
                    'severity': severity,
                    'error_message': str(exc)[:500],
                    'fingerprint': _fingerprint(exc),
                    'context': {k: str(v)[:200] for k, v in context.items()},
                },
                spark_budget=50,
                created_by='error_advice',
            )
    except Exception as e:
        # Never let the remediation path crash the failure path
        logger.debug(f"Agent remediation goal creation failed: {e}")


def handle_exception(
    exc: BaseException,
    *,
    category: str,
    severity: str = 'medium',
    agent_remediation: bool = False,
    context: Optional[dict] = None,
) -> None:
    """Central dispatch for a caught exception.  Used by the
    @error_advice decorator and the with-block context manager; safe
    to call directly from any handler that already caught its own
    exception but wants the central fan-out (logging + Sentry +
    agent goal).

    severity is one of 'low' / 'medium' / 'high' / 'critical' — drives
    the agent goal's spark_budget and the operator-side alerting.
    """
    ctx = dict(context or {})
    if not _should_emit(category, exc):
        # Throttled — log at debug only so we don't spam
        logger.debug(
            f"[error_advice/{category}] suppressed-by-throttle "
            f"{type(exc).__name__}: {exc}"
        )
        return

    logger.error(
        f"[error_advice/{category}] {type(exc).__name__}: {exc}",
        exc_info=exc,
        extra={'category': category, 'severity': severity, **ctx},
    )
    _try_sentry_capture(exc, category, ctx)
    if agent_remediation:
        _try_agent_remediation(category, exc, ctx, severity)


def error_advice(
    category: str,
    *,
    severity: str = 'medium',
    agent_remediation: bool = False,
    reraise: bool = True,
    context_extractor: Optional[Callable[..., dict]] = None,
) -> Callable:
    """Decorator: wrap a function so any uncaught exception inside it
    fans out to the central error_advice dispatch.

    Args:
        category:           Short string identifying the failure
                            domain (e.g. 'tts.install', 'tts.synth',
                            'llm.onboard', 'social.feed').  Drives
                            throttling + agent-goal categorization.
        severity:           'low' | 'medium' | 'high' | 'critical'.
        agent_remediation:  True ⇒ create an AgentGoal for an autogen
                            agent to pick up beyond the deterministic
                            recovery the wrapped function tried.
        reraise:            True (default) ⇒ exception still bubbles
                            up after central dispatch (additive
                            observation).  False ⇒ swallow + return
                            None (rare; only for true side-effect
                            functions where caller can't act on the
                            failure).
        context_extractor:  Optional `fn(*args, **kwargs) -> dict`
                            that pulls extra structured context
                            (e.g. backend name, user_id) out of the
                            wrapped call's args.  Defaults to no
                            extraction.

    Usage:
        @error_advice('tts.install', agent_remediation=True)
        def install_backend_packages(backend, ...):
            ...

        # Captures backend, user_id from kwargs:
        @error_advice('tts.synth',
                      context_extractor=lambda *a, **kw:
                          {'backend': kw.get('backend'),
                           'user_id': kw.get('user_id')})
        def synthesize(...):
            ...
    """
    def _decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — the whole point
                ctx: dict[str, Any] = {'function': fn.__qualname__}
                if context_extractor is not None:
                    try:
                        ctx.update(context_extractor(*args, **kwargs))
                    except Exception:
                        pass
                handle_exception(
                    exc,
                    category=category,
                    severity=severity,
                    agent_remediation=agent_remediation,
                    context=ctx,
                )
                if reraise:
                    raise
                return None
        return _wrapper
    return _decorate


@contextmanager
def error_advice_block(
    category: str,
    *,
    severity: str = 'medium',
    agent_remediation: bool = False,
    reraise: bool = True,
    context: Optional[dict] = None,
) -> Iterator[None]:
    """Context-manager form for callers that want to wrap an inline
    block instead of a whole function.  Same semantics as the
    decorator — try/except, central dispatch, reraise (or not)."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        handle_exception(
            exc,
            category=category,
            severity=severity,
            agent_remediation=agent_remediation,
            context=context,
        )
        if reraise:
            raise
