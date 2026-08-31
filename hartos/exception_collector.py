"""
Centralized Exception Collector
================================

Thread-safe singleton that aggregates exceptions from anywhere in the codebase.
Used by SelfHealingDispatcher and ExceptionWatcher to detect recurring patterns
and create automated fix goals.

All recording is fire-and-forget — never raises into the main execution flow.
"""
import time
import uuid
import threading
import traceback
import logging
from collections import defaultdict
from typing import List, Dict, Optional, Callable, Any

logger = logging.getLogger(__name__)


class ExceptionRecord:
    """Single exception occurrence."""

    __slots__ = (
        'id', 'exc_type', 'exc_message', 'module', 'function',
        'lineno', 'traceback_str', 'timestamp', 'user_prompt',
        'action_id', 'context', 'resolved',
    )

    def __init__(self, exc_type: str, exc_message: str, module: str = '',
                 function: str = '', lineno: int = 0, traceback_str: str = '',
                 user_prompt: str = '', action_id: int = 0,
                 context: Optional[Dict] = None):
        self.id = uuid.uuid4().hex[:12]
        self.exc_type = exc_type
        self.exc_message = exc_message
        self.module = module
        self.function = function
        self.lineno = lineno
        self.traceback_str = traceback_str
        self.timestamp = time.time()
        self.user_prompt = user_prompt
        self.action_id = action_id
        self.context = context or {}
        self.resolved = False

    @property
    def pattern_key(self) -> str:
        """Grouping key for deduplication: (type, module, function)."""
        return f"{self.exc_type}::{self.module}::{self.function}"

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'exc_type': self.exc_type,
            'exc_message': self.exc_message,
            'module': self.module,
            'function': self.function,
            'lineno': self.lineno,
            'traceback_str': self.traceback_str,
            'timestamp': self.timestamp,
            'user_prompt': self.user_prompt,
            'action_id': self.action_id,
            'context': self.context,
            'resolved': self.resolved,
            'pattern_key': self.pattern_key,
        }


class ExceptionCollector:
    """Thread-safe centralized exception aggregation.

    Singleton. Bounded buffer (drops oldest beyond max_buffer).
    Supports subscriber callbacks for real-time notification.
    """

    _instance = None
    _create_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.RLock()
        self._exceptions: List[ExceptionRecord] = []
        self._max_buffer = int(500)
        self._subscribers: List[Callable[[ExceptionRecord], None]] = []

    @classmethod
    def get_instance(cls) -> 'ExceptionCollector':
        if cls._instance is None:
            with cls._create_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # Backwards-compatible alias — some call sites (mcp_http_bridge,
    # older integrations) use `ExceptionCollector.instance()` without
    # the `get_` prefix.  Route both through the same singleton getter
    # so the spellings never diverge and we don't silently create two
    # collectors.  Keep ONE source of truth; do not copy the lock /
    # double-check pattern above into a sibling method.
    @classmethod
    def instance(cls) -> 'ExceptionCollector':
        return cls.get_instance()

    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing only)."""
        with cls._create_lock:
            cls._instance = None

    def record(self, exc: BaseException, module: str = '', function: str = '',
               user_prompt: str = '', action_id: int = 0,
               context: Optional[Dict] = None):
        """Record an exception. Called from except blocks.

        Creates ExceptionRecord from the exception + current traceback.
        Appends to buffer (circular — drops oldest beyond max_buffer).
        Notifies all subscribers.
        """
        try:
            tb_str = traceback.format_exc()
            if tb_str == 'NoneType: None\n':
                tb_str = str(exc)

            record = ExceptionRecord(
                exc_type=type(exc).__name__,
                exc_message=str(exc),
                module=module,
                function=function,
                lineno=0,
                traceback_str=tb_str,
                user_prompt=user_prompt,
                action_id=action_id,
                context=context,
            )

            # Extract line number from traceback if available
            tb = getattr(exc, '__traceback__', None)
            if tb is not None:
                while tb.tb_next:
                    tb = tb.tb_next
                record.lineno = tb.tb_lineno

            with self._lock:
                self._exceptions.append(record)
                # Circular buffer: drop oldest
                if len(self._exceptions) > self._max_buffer:
                    self._exceptions = self._exceptions[-self._max_buffer:]

            # Notify subscribers (outside lock)
            for callback in self._subscribers:
                try:
                    callback(record)
                except Exception:
                    pass

        except Exception:
            pass  # never let the collector itself crash

    def subscribe(self, callback: Callable[[ExceptionRecord], None]):
        """Register a callback for real-time exception notification."""
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        """Remove a subscriber."""
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not callback]

    def get_unresolved(self, since: float = 0) -> List[ExceptionRecord]:
        """Get exceptions since timestamp that haven't been resolved."""
        with self._lock:
            return [
                r for r in self._exceptions
                if not r.resolved and r.timestamp >= since
            ]

    def get_patterns(self, since: float = 0,
                     min_count: int = 1) -> Dict[str, List[ExceptionRecord]]:
        """Group unresolved exceptions by pattern_key.

        Returns only groups with >= min_count occurrences.
        """
        unresolved = self.get_unresolved(since=since)
        groups: Dict[str, List[ExceptionRecord]] = defaultdict(list)
        for rec in unresolved:
            groups[rec.pattern_key].append(rec)
        return {k: v for k, v in groups.items() if len(v) >= min_count}

    def mark_resolved(self, record_ids: List[str]):
        """Mark exceptions as resolved (fix goal created)."""
        id_set = set(record_ids)
        with self._lock:
            for rec in self._exceptions:
                if rec.id in id_set:
                    rec.resolved = True

    def mark_pattern_resolved(self, pattern_key: str):
        """Mark all exceptions matching a pattern as resolved."""
        with self._lock:
            for rec in self._exceptions:
                if rec.pattern_key == pattern_key:
                    rec.resolved = True

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate stats: count by type, top modules, frequency."""
        with self._lock:
            total = len(self._exceptions)
            unresolved = sum(1 for r in self._exceptions if not r.resolved)
            resolved = total - unresolved

            by_type: Dict[str, int] = defaultdict(int)
            by_module: Dict[str, int] = defaultdict(int)
            for rec in self._exceptions:
                if not rec.resolved:
                    by_type[rec.exc_type] += 1
                    by_module[rec.module] += 1

            top_types = sorted(by_type.items(), key=lambda x: -x[1])[:10]
            top_modules = sorted(by_module.items(), key=lambda x: -x[1])[:10]

            return {
                'total': total,
                'unresolved': unresolved,
                'resolved': resolved,
                'buffer_capacity': self._max_buffer,
                'top_exception_types': top_types,
                'top_modules': top_modules,
            }

    def clear(self):
        """Clear all records (for testing)."""
        with self._lock:
            self._exceptions.clear()


def record_exception(exc: BaseException, module: str = '', function: str = '',
                     user_prompt: str = '', action_id: int = 0, **ctx):
    """Convenience function — fire-and-forget exception recording.

    Safe to call anywhere. Never raises.
    """
    try:
        ExceptionCollector.get_instance().record(
            exc, module=module, function=function,
            user_prompt=user_prompt, action_id=action_id, context=ctx)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# Reusable auto-wrap mixin: every subsystem inherits this once and
# all its method failures auto-feed the self-heal pipeline.  No
# bespoke per-subsystem report calls; no per-method except-block
# edits.  Used by ChannelAdapter, VisionBackend, LLM workers,
# daemon supervisors, dynamic tool registries — anywhere a class
# represents a subsystem with installable / failable behaviour.
# ────────────────────────────────────────────────────────────────────

_AUTO_REPORT_WRAPPED_ATTR = '__subsystem_failure_wrapped__'


def _wrap_async_with_subsystem_report(method, subsystem: str,
                                      method_name: str):
    """Wrap an async method so escaping exceptions feed the
    canonical self-heal pipeline.  Re-raises unchanged."""
    import asyncio  # noqa: F401  — referenced indirectly by `await`
    import functools

    @functools.wraps(method)
    async def _wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except Exception as exc:
            try:
                identifier = self._identifier_for_self_heal()
                report_subsystem_failure(
                    subsystem, identifier, exc, method_name)
            except Exception:
                pass  # sink failure must not change caller contract
            raise
    return _wrapped


def _wrap_sync_with_subsystem_report(method, subsystem: str,
                                     method_name: str):
    """Sync counterpart — for subsystems whose methods aren't async."""
    import functools

    @functools.wraps(method)
    def _wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            try:
                identifier = self._identifier_for_self_heal()
                report_subsystem_failure(
                    subsystem, identifier, exc, method_name)
            except Exception:
                pass
            raise
    return _wrapped


class AutoReportSubsystemFailures:
    """Mixin: auto-wrap configured methods so escaping exceptions feed
    the canonical self-heal pipeline.

    Subclass contract:
      - Set class attr ``SUBSYSTEM = 'tts' | 'channels' | 'vlm' | 'llm'
        | 'daemon' | 'tool'`` (lowercase, no dot — the helper splits
        on '.').
      - Set class attr ``AUTO_REPORTED_METHODS = ('start', 'load',
        'send_message', ...)`` listing concrete method names that
        should have their exceptions auto-pushed.
      - Optionally override ``_identifier_for_self_heal(self)`` to
        return a per-instance identifier (default: ``self.name`` if
        the attribute exists, else the class name).

    All listed methods on any concrete subclass get transparently
    wrapped at class-load time by ``__init_subclass__``.  Wrapping is
    idempotent — re-importing the module does NOT stack wrappers.
    Async-vs-sync is auto-detected via ``inspect.iscoroutinefunction``.

    The wrap re-raises the original exception — caller contracts are
    unchanged.  Sink failures (collector unavailable, etc.) are
    swallowed and logged at DEBUG — observability MUST NOT become a
    new failure surface.

    Why this mixin instead of per-subsystem decorators / explicit
    pushes:
      - One place to enforce the (subsystem, identifier) shape that
        SelfHealingDispatcher's pattern_key grouping depends on.
      - Zero changes to concrete subclass code beyond setting the
        two class attrs.  Subclasses can also call
        ``report_subsystem_failure`` directly from inline try/except
        blocks where they swallow the exception (e.g. return
        ``SendResult(success=False)`` and don't propagate).
      - The same auto-wrap behaviour the user requested for ALL
        venv-based model setup backend failures + ALL dynamic tool
        registration failures + ALL daemon supervisors — instead of
        adding bespoke wiring per surface.
    """

    SUBSYSTEM: str = ''
    AUTO_REPORTED_METHODS: tuple = ()

    def _identifier_for_self_heal(self) -> str:
        """Per-instance identifier passed to the canonical helper.
        Default: ``self.name`` (matches ChannelAdapter / VisionBackend
        / similar patterns); falls back to class name when missing."""
        name_attr = getattr(self, 'name', None)
        if callable(name_attr):
            try:
                return str(name_attr())
            except Exception:
                pass
        if isinstance(name_attr, str) and name_attr:
            return name_attr
        return type(self).__name__

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        subsystem = cls.SUBSYSTEM
        if not subsystem:
            # Intermediate base classes that don't set SUBSYSTEM
            # (e.g. ChannelAdapter itself) just pass through —
            # the leaf class that sets SUBSYSTEM gets the wrap.
            return
        import inspect
        for method_name in cls.AUTO_REPORTED_METHODS:
            method = cls.__dict__.get(method_name)
            if method is None:
                continue  # subclass didn't override this method
            if getattr(method, _AUTO_REPORT_WRAPPED_ATTR, False):
                continue  # already wrapped — idempotent
            if inspect.iscoroutinefunction(method):
                wrapped = _wrap_async_with_subsystem_report(
                    method, subsystem, method_name)
            else:
                wrapped = _wrap_sync_with_subsystem_report(
                    method, subsystem, method_name)
            setattr(wrapped, _AUTO_REPORT_WRAPPED_ATTR, True)
            setattr(cls, method_name, wrapped)


def report_subsystem_failure(subsystem: str, identifier: str,
                             exc: BaseException, function: str,
                             **ctx) -> None:
    """Single canonical entry point for ANY subsystem's silent-failure
    surface to feed the self-heal pipeline.

    Subsystems with localized circuit breakers / demotion state /
    install retries (TTS backends, channel adapters, VLM/LLM model
    workers, daemon supervisors, dynamic tool registrations) all
    need their failures to ALSO reach the canonical self-heal chain:

        record_exception() → ExceptionCollector (singleton, bounded
        buffer, dedup by pattern_key) → ExceptionWatcher (runs
        inside agent_daemon._tick, classifies severity) →
        SelfHealingDispatcher (creates a `self_heal` goal for an
        idle agent when pattern crosses threshold) → idle agent
        uses repair tools to fix the root cause.

    Without this single entry, each subsystem would maintain its
    own opinion of "module key" and ctx vocabulary — a parallel-
    path violation that splinters the pattern_key grouping the
    dispatcher relies on for dedup.  Use THIS helper instead:

      report_subsystem_failure('tts',     'indic_parler', exc, 'demote', ...)
      report_subsystem_failure('channels','discord',      exc, 'send_message', ...)
      report_subsystem_failure('vlm',     'minicpm',      exc, 'load',  ...)
      report_subsystem_failure('llm',     'qwen3_4b',     exc, 'load',  ...)
      report_subsystem_failure('daemon',  'agent_daemon', exc, 'tick',  ...)
      report_subsystem_failure('tool',    'pdf_extract',  exc, 'register', ...)

    Module key is uniformly ``{subsystem}.{identifier}`` so the
    dispatcher's pattern_key = ``{exc_type}::{subsystem}.{identifier}
    ::{function}`` groups related failures correctly across the
    entire system.  Self-heal goals carry the subsystem in their
    config so the agent picks the right repair tool from the
    capability matrix.

    Fire-and-forget — never raises into caller.  Safe from any
    except block.
    """
    try:
        ExceptionCollector.get_instance().record(
            exc,
            module=f'{subsystem}.{identifier}' if identifier else subsystem,
            function=function,
            context={'subsystem': subsystem, 'identifier': identifier, **ctx},
        )
    except Exception:
        pass
