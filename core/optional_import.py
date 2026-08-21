"""
core.optional_import — logged graceful degradation for optional dependencies.

WHY THIS EXISTS
───────────────
Nunba had at least 6 `try: import X; except ImportError: pass` blocks
sprinkled across main.py and adjacent modules.  Each one silently swallowed
the failure with no log line, no metric, no operator-visible signal.  When
a feature mysteriously stopped working in a frozen build (because a wheel
got pruned, a sys.path entry shifted, or a transitive dep flipped backend),
there was NO way to find out from the running process — operators had to
add print statements and rebuild.

This module replaces that pattern with a single helper that:
  1. Attempts the import.
  2. On failure, logs ONE INFO-level line with the human-readable reason
     (so it lands in the standard log, surfaces in /api/admin/logs).
  3. Records the degradation in a process-global registry queryable via
     `/api/admin/diag/degradations` (operator self-service diagnosis).
  4. Returns a sentinel/fallback so the call site can keep using the name
     without `if X is None:` peppering.
  5. Is idempotent — the second call for the same module is silent.

DESIGN NOTES
────────────
- We log at INFO, not WARNING.  These are EXPECTED degradations (e.g.,
  optional GPU libs missing on a CPU-only laptop).  WARNING would cry-wolf
  and train operators to ignore the channel.
- The registry is a plain dict — no thread safety needed because all
  imports happen at module-load time on the main thread.
- We do NOT cache the imported module here; let Python's normal import
  machinery do that.  This module ONLY tracks failures.
- The Flask blueprint exposes the registry as JSON; gated by
  `require_local_or_token` upstream because degradation lists can leak
  the absence of paid-tier integrations (an information-disclosure vector
  on multi-tenant deploys, irrelevant on flat).
"""
from __future__ import annotations

import importlib
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Registry of failed imports — process-global, single-source-of-truth for
# the /api/admin/diag/degradations endpoint.
#   key:   module name (e.g., "integrations.service_tools.vram_manager")
#   value: {reason, error, ts, attempts}
_DEGRADED: Dict[str, Dict[str, Any]] = {}

# Modules we already successfully imported — used to short-circuit repeat
# calls so the cost of `optional_import('foo', ...)` in a hot path is one
# dict lookup, not a fresh importlib walk.
_LOADED: Dict[str, Any] = {}


def optional_import(
    module_name: str,
    reason: str,
    fallback: Any = None,
) -> Any:
    """Import a module by name; on failure log + register and return fallback.

    Args:
        module_name: Dotted import path, e.g. ``'integrations.service_tools.vram_manager'``.
        reason: Human-readable why-this-is-optional, used in the log line
            AND surfaced in `/api/admin/diag/degradations`.  Examples:
            ``'GPU VRAM telemetry'``, ``'HF Hub model search'``,
            ``'WAMP ticket auth'``.  Be specific — "feature unavailable"
            is unhelpful.
        fallback: Value returned when the import fails.  Defaults to None.
            Pass a stub class or no-op module if call sites need attribute
            access.

    Returns:
        The imported module on success, or `fallback` on ImportError /
        any other exception during import.

    Idempotency:
        Successful imports are cached in `_LOADED` and returned directly
        on subsequent calls (no re-import).  Failed imports increment an
        attempt counter in `_DEGRADED` but do NOT re-log — first failure
        is the signal, subsequent retries are noise.
    """
    if module_name in _LOADED:
        return _LOADED[module_name]

    if module_name in _DEGRADED:
        _DEGRADED[module_name]['attempts'] += 1
        return fallback

    try:
        mod = importlib.import_module(module_name)
        _LOADED[module_name] = mod
        return mod
    except Exception as e:
        # Catch broad — `ImportError` misses things like circular-import
        # `AttributeError` and missing-DLL `OSError` on Windows.
        _DEGRADED[module_name] = {
            'reason': reason,
            'error': f"{type(e).__name__}: {e}",
            'ts': time.time(),
            'attempts': 1,
        }
        # ONE log line per module — INFO level (expected degradation, not
        # a panic).  WARNING would cry-wolf for legitimate optional deps.
        logger.info(
            "optional_import: %s unavailable (%s) — %s: %s",
            module_name, reason, type(e).__name__, e,
        )
        return fallback


class _LazyModule:
    """A proxy that imports its target module on first attribute access.

    WHY
    ───
    Some heavy modules (``autogen`` drags ``google.api_core`` 7.6s +
    ``flaml`` + the contrib capabilities chain -> ``llmlingua`` ->
    ``torch`` 4.2s) are imported at a module's TOP LEVEL but only USED
    inside functions.  That cost lands on every ``import <module>`` —
    and transitively on the whole backend boot — even when the agent-
    building code path is never reached in a given process.

    This proxy lets a module write::

        autogen = lazy_module("autogen")

    at top level, keep every existing ``autogen.AssistantAgent(...)``
    call site BYTE-FOR-BYTE unchanged, and pay the import cost only on
    the first real attribute access (i.e. when an agent is actually
    constructed).

    Unlike :func:`optional_import`, there is NO fallback sentinel — the
    target is a REQUIRED dependency at use-time, so a missing module
    must raise loudly (ImportError) at first access, exactly as an eager
    ``import`` would have.  ``lazy_module`` is purely a *when*, not a
    *whether*.

    Thread-safety: the first-access import is guarded so concurrent
    callers don't double-import; Python's import lock makes the actual
    ``import_module`` idempotent, the local lock only avoids a redundant
    second call and a torn ``_mod`` read.
    """

    # Slots keep the proxy lean and ensure __getattr__ is the ONLY way
    # to reach module attributes (no accidental shadowing).
    __slots__ = ("_name", "_mod", "_lock", "_on_import")

    def __init__(self, name: str, on_import: Optional[Callable[[], None]] = None):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mod", None)
        object.__setattr__(self, "_on_import", on_import)
        import threading
        object.__setattr__(self, "_lock", threading.Lock())

    def _resolve(self) -> Any:
        mod = object.__getattribute__(self, "_mod")
        if mod is not None:
            return mod
        lock = object.__getattribute__(self, "_lock")
        with lock:
            mod = object.__getattribute__(self, "_mod")
            if mod is None:
                name = object.__getattribute__(self, "_name")
                mod = importlib.import_module(name)
                object.__setattr__(self, "_mod", mod)
                # Fire the post-import hook ONCE, under the same lock that
                # guards the import, so concurrent first-callers can't both
                # run it.  Swallowing here is deliberate: the hook is a
                # nicety (e.g. installing autogen's crash-proof IOStream);
                # a broken hook must never turn a working import into an
                # ImportError at a call site that only asked for a module.
                hook = object.__getattribute__(self, "_on_import")
                if hook is not None:
                    object.__setattr__(self, "_on_import", None)
                    try:
                        hook()
                    except Exception:
                        logger.info(
                            "lazy_module(%s): on_import hook failed", name,
                            exc_info=True,
                        )
            return mod

    # The three private slot names — set/get/del of these stay LOCAL to the
    # proxy; everything else forwards to the resolved module.  This makes the
    # proxy transparent to attribute manipulation, including unittest.mock's
    # ``patch('mod.autogen.SomeAttr')`` which does getattr/setattr/delattr on
    # the proxy to install + restore the mock — those must land on the REAL
    # module so call sites (and the patched code) see the mock.
    __slot_names = ("_name", "_mod", "_lock", "_on_import")

    def __getattr__(self, item: str) -> Any:
        # __getattr__ only fires for names NOT found normally; with
        # __slots__ that means everything except the private slots, so
        # this is the module-attribute forwarding path.
        return getattr(self._resolve(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in _LazyModule.__slot_names:
            object.__setattr__(self, key, value)
        else:
            setattr(self._resolve(), key, value)

    def __delattr__(self, key: str) -> None:
        if key in _LazyModule.__slot_names:
            object.__delattr__(self, key)
        else:
            delattr(self._resolve(), key)

    def __dir__(self):
        return dir(self._resolve())

    def __repr__(self):
        mod = object.__getattribute__(self, "_mod")
        state = "loaded" if mod is not None else "deferred"
        return "<lazy_module %r (%s)>" % (
            object.__getattribute__(self, "_name"), state,
        )


def lazy_module(name: str, on_import: Optional[Callable[[], None]] = None) -> Any:
    """Return a proxy that imports ``name`` on first attribute access.

    Args:
        name: Dotted import path of a REQUIRED-at-use-time module that is
            expensive to import and only used inside functions, e.g.
            ``"autogen"`` or
            ``"autogen.agentchat.contrib.capabilities.transform_messages"``.
        on_import: Optional zero-arg callable run ONCE, immediately after
            the real import succeeds.  For setup that needs the module
            loaded but must not itself force the import — the case this
            exists for is installing autogen's crash-proof IOStream (see
            :func:`core.io_guard.install_autogen_iostream`).  Exceptions
            from the hook are logged and swallowed.

    Returns:
        A :class:`_LazyModule` proxy.  Accessing any attribute on it
        triggers the real import (raising ImportError if truly missing,
        same as an eager import) and forwards the attribute.

    NOT for optional deps — use :func:`optional_import` when a missing
    module should degrade gracefully with a fallback.  ``lazy_module``
    only defers *when* a present module is imported, never *whether*.
    """
    return _LazyModule(name, on_import=on_import)


def list_degradations() -> List[Dict[str, Any]]:
    """Return a snapshot of all registered degradations for the admin endpoint.

    Output is a list of dicts with stable keys so the frontend can render
    a table without per-field probing.  Sorted by first-failure timestamp
    so the UI naturally shows boot-time degradations first."""
    out = []
    for name, info in _DEGRADED.items():
        out.append({
            'module': name,
            'reason': info['reason'],
            'error': info['error'],
            'first_failed_at': info['ts'],
            'attempts': info['attempts'],
        })
    out.sort(key=lambda d: d['first_failed_at'])
    return out


def is_available(module_name: str) -> bool:
    """Cheap predicate for call sites that need a boolean check before
    invoking a feature — avoids the fallback-sentinel dance."""
    return module_name in _LOADED


def reset_for_tests() -> None:
    """Clear both registries.  Test-only helper — do NOT call from app code."""
    _DEGRADED.clear()
    _LOADED.clear()


__all__ = [
    'optional_import',
    'lazy_module',
    'list_degradations',
    'is_available',
    'reset_for_tests',
]
