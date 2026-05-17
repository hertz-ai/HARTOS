"""Singleton accessor for the canonical hart_intelligence modules.

Worker threads must NOT do ``from hart_intelligence import X`` directly.
The hart_intelligence module pulls in transformers / langchain / autogen
on first import — a chain that holds Python's per-module import lock for
minutes on cold disk.  When a worker thread races the canonical loader
(``hartos_backend_adapter._attempt_hartos_init``) on that same lock, the
worker stalls forever — the symptom shape is `Tier-1
ACTIVE` never logs, chat falls through to fallback path, daemon's
``is_user_recently_active()`` gate is unreachable, MMLU runaway can't be
stopped, etc).

The singleton is ``sys.modules['hart_intelligence']`` — Python guarantees
a single dict entry per module.  ONE writer (the canonical loader) does
the slow import.  Every other reader uses this accessor.  No new lock,
no new mechanism — we're just naming the singleton that already exists.

Observability
=============
Every resolution is logged so silent no-ops are never invisible:

* HIT  — symbol resolved, returned to caller (DEBUG, with module name).
* MISS — HARTOS not yet loaded; first miss per symbol → INFO (loud once),
         subsequent identical misses → DEBUG (quiet).  This way the
         logs show the *transition* without flooding when many workers
         hit the same not-yet-loaded symbol every tick.
* MISS_ATTR — modules are loaded but the requested attribute isn't
              bound (mid-init or symbol typo).  Always INFO — distinct
              from "loader hasn't started" because the diagnosis is
              different.

Usage
=====
::

    from core.safe_hartos_attr import safe_hartos_attr

    publish_async = safe_hartos_attr('publish_async')
    if publish_async is not None:
        publish_async(topic, msg)
    # else: HARTOS not yet loaded — caller no-ops, same as the
    # `try: from hart_intelligence import …; except ImportError: pass`
    # pattern this replaces, but without triggering the import lock.

Enforcement
===========
``[tool.ruff.lint.flake8-tidy-imports.banned-api]`` in HARTOS
``pyproject.toml`` bans ``from hart_intelligence import …`` and
``from hart_intelligence_entry import …`` everywhere except the
canonical loader (which carries ``# noqa: TID251`` with a comment).
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Any

logger = logging.getLogger('core.safe_hartos_attr')

_FACADE = 'hart_intelligence'
_ENTRY = 'hart_intelligence_entry'

# Tracks (symbol_name, was_loaded_at_check_time) tuples we've already
# logged at INFO.  Subsequent identical misses fall to DEBUG so a worker
# polling every tick doesn't flood the log.  Reset implicitly on process
# restart; cleared explicitly by ``reset_log_dedup()`` for tests.
_logged_states: set[tuple[str, str]] = set()
_dedup_lock = threading.Lock()


def _log_outcome(name: str, outcome: str, *, mod_name: str | None = None) -> None:
    """Log a resolution outcome, deduplicating identical (symbol, outcome) pairs.

    First occurrence at INFO, subsequent at DEBUG — gives a loud signal
    on state transitions (loader finishes, symbol becomes resolvable)
    while keeping steady-state ticks quiet.
    """
    key = (name, outcome)
    with _dedup_lock:
        first = key not in _logged_states
        if first:
            _logged_states.add(key)
    if outcome == 'HIT':
        # HITs are normal once HARTOS is up — DEBUG always, single
        # transition log on first hit per symbol so we can see "loader
        # came online" in the timeline.
        if first:
            logger.info(
                "safe_hartos_attr HIT name=%s mod=%s (first resolution — "
                "HARTOS now reachable for this symbol)",
                name, mod_name,
            )
        else:
            logger.debug("safe_hartos_attr HIT name=%s mod=%s", name, mod_name)
    elif outcome == 'MISS':
        if first:
            logger.info(
                "safe_hartos_attr MISS name=%s — HARTOS not yet loaded "
                "(canonical loader hartos_backend_adapter._attempt_hartos_init "
                "hasn't published sys.modules['hart_intelligence']). "
                "Caller will no-op gracefully; subsequent identical misses "
                "logged at DEBUG.",
                name,
            )
        else:
            logger.debug("safe_hartos_attr MISS name=%s", name)
    elif outcome == 'MISS_ATTR':
        # Different bug shape from MISS: modules are loaded but symbol
        # not bound. Always INFO — caller likely typo'd or symbol moved.
        logger.info(
            "safe_hartos_attr MISS_ATTR name=%s mod=%s — module loaded "
            "but attribute not bound (typo? renamed? mid-init?)",
            name, mod_name,
        )


def safe_hartos_attr(name: str, default: Any = None) -> Any:
    """Read a symbol from the loaded hart_intelligence singleton.

    Resolution order:
      1. ``sys.modules['hart_intelligence']`` (the canonical facade)
      2. ``sys.modules['hart_intelligence_entry']`` (the implementation)
      3. ``default`` if neither is loaded yet

    The facade re-exports everything from the entry module via
    ``from hart_intelligence_entry import *``, so once the loader has
    finished, both names point to the same symbol set — but during early
    boot only the entry module may be partially loaded.  Checking both
    keeps callers working at every loader stage without a single false
    ImportError on the cold path.

    NEVER triggers an import.  If neither module is in ``sys.modules``,
    returns ``default``.  Caller is expected to handle ``None``/``default``
    by no-op'ing — exactly the shape ``idle_detection.get_idle_opted_in_agents``
    and ``world_model_bridge._init_in_process`` already use.

    Every call is logged (deduplicated) so silent fall-throughs are
    never invisible — see module docstring for log levels.

    Args:
        name: attribute name to look up (e.g. ``'publish_async'``).
        default: value to return if HARTOS isn't loaded or the attribute
            doesn't exist.  Defaults to ``None``.

    Returns:
        The attribute value, or ``default``.
    """
    facade = sys.modules.get(_FACADE)
    entry = sys.modules.get(_ENTRY)
    mod = facade or entry
    if mod is None:
        _log_outcome(name, 'MISS')
        return default
    mod_name = _FACADE if facade is not None else _ENTRY
    val = getattr(mod, name, None)
    if val is None:
        _log_outcome(name, 'MISS_ATTR', mod_name=mod_name)
        return default
    _log_outcome(name, 'HIT', mod_name=mod_name)
    return val


def hartos_loaded() -> bool:
    """True if the canonical loader has finished and the facade is ready.

    Cheaper than ``safe_hartos_attr`` for "is it ready?" checks where the
    caller doesn't need a specific symbol — no ``getattr`` traversal,
    just a dict lookup.  Use this for short-circuit gates::

        if not hartos_loaded():
            return  # nothing for us to do yet

    A ``True`` result means at least one of the two module names is in
    ``sys.modules``; it does NOT promise every symbol the loader will
    eventually publish is bound yet (mid-init reads can still see
    ``None`` from ``safe_hartos_attr``).  Use ``safe_hartos_attr`` with
    a defensive ``if x is not None`` for the actual call.
    """
    return _FACADE in sys.modules or _ENTRY in sys.modules


def reset_log_dedup() -> None:
    """Clear the (symbol, outcome) dedup memory.

    Tests use this to assert on first-time INFO emission without process
    restart noise.  Production code should not call this — repeated INFO
    logs of the same state transition are pure noise on the chat hot path.
    """
    with _dedup_lock:
        _logged_states.clear()
