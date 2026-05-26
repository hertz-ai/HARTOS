"""
HevolveSocial — global tenant filter for SQLAlchemy ORM queries.

Phase 7a / Phase 8. Plan reference: sunny-gliding-eich.md, Part C.1 + Part E.1.

Migration v40 (`migrate_to_v40_tenancy`) added a nullable `tenant_id`
column to ~34 social tables via raw `ALTER TABLE`. The columns exist
in the SQL schema but were never declared on the Python ORM model
classes — so without this module, every ORM query would silently
ignore them and return every tenant's rows undifferentiated.

Strict mode (Phase 8) — rollback semantics:
  Toggling `tenant_strict_mode` on or off is bit-for-bit reversible
  at the SQL layer.  The listener only ADDS a WHERE clause; it never
  mutates any row.  The `before_flush` auto-stamp continues to write
  `tenant_id = g.tenant_id` on inserts in BOTH modes, so no new
  untenanted rows appear once strict is on.  Flipping back to loose
  immediately restores visibility of pre-v40 NULL rows.  Operators
  can toggle the per-tenant override safely without DB mutations.

Strict mode — env-var caveat:
  `HEVOLVE_FLAG_TENANT_STRICT_MODE` is a PROCESS-GLOBAL fallback
  used when no Flask request context is active (daemon threads,
  scripts, tests).  In production, per-request `g.feature_flags`
  takes priority — set per-tenant via the `tenant_feature_flags`
  table.  Don't flip the env var on a multi-tenant cloud node
  unless you intend it for every tenant on that process.

This module fixes that without modifying the external `hevolve-database`
repo (`sql.models`) or the local fallback (`_models_local.py`):

  1. `register_tenant_aware(cls)` runtime-augments the ORM mapper with
     a `tenant_id` Column attribute. The SQL column already exists; we
     just teach SQLAlchemy about it.

  2. `install_tenant_filter()` installs two SQLAlchemy event listeners
     on the global Session class:

       a. `do_orm_execute` — injects `with_loader_criteria` on every
          SELECT for tenant-aware classes. Filter shape is:
            `tenant_id == g.tenant_id  OR  tenant_id IS NULL`
          The NULL pass-through means flat/regional rows (untenanted)
          remain visible in cloud mode for backward compatibility.

       b. `before_flush` — auto-stamps `tenant_id = g.tenant_id` on
          every INSERT for tenant-aware classes that don't already have
          it set, so apps don't need to remember to set it.

  3. Both listeners no-op outside Flask request context (tests, daemon
     threads) and when `g.tenant_id` is None (flat/regional). This is
     the property that makes the listener regression-safe — adding it
     to a single-tenant deploy changes no behavior.

Usage (called once at startup, after models are imported):

    from integrations.social.tenant_filter import (
        install_tenant_filter, register_tenant_aware,
    )
    from integrations.social.models import (
        User, Post, Comment, Community, Notification,
        CommunityMembership, Vote, Follow,
    )
    install_tenant_filter()
    for cls in (User, Post, Comment, Community, Notification,
                CommunityMembership, Vote, Follow):
        register_tenant_aware(cls)

Idempotent — calling either function twice is a no-op.

Defense in depth:
  This is the SAME defensive layer the WAMP per-tenant ACL provides on
  the realtime side. They cover different attack surfaces — WAMP gates
  cross-tenant subscribe, the ORM filter gates cross-tenant query.
  Both must be installed for a multi-tenant cloud deploy.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Type

from sqlalchemy import Column, String, event, inspect, or_, and_, true, false
from sqlalchemy.orm import Session, with_loader_criteria

logger = logging.getLogger('hevolve_social')

# Whitelist of tenant-aware classes. Populated by register_tenant_aware().
# Order doesn't matter — the listener iterates and applies a criterion
# per class on every query.
_TENANT_AWARE: List[Type] = []
_TENANT_AWARE_LOCK = threading.Lock()

# Sentinel: True once install_tenant_filter() has wired the listeners
# onto the global Session class. Idempotent.
#
# Pass-1 M4 note — idempotency contract:
#   Both `install_tenant_filter()` and `register_tenant_aware()` are
#   safe to call multiple times.  install_tenant_filter short-circuits
#   on the second call via _INSTALLED; register_tenant_aware
#   short-circuits via the `cls in _TENANT_AWARE` check.  The listener
#   itself reads _TENANT_AWARE on every query, so registering more
#   classes after install_tenant_filter has already fired works
#   correctly — no need to re-install.  Test fixtures in this repo
#   exploit this pattern: `models_mod._engine = None` between tests
#   resets the engine, but the Session-class listener stays attached
#   across tests, so it picks up the fresh engine's queries
#   automatically.
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def _augment_class(cls: Type) -> None:
    """Teach SQLAlchemy about the `tenant_id` column on `cls`.

    The migration v40 created the column in the SQL schema; we just
    need to declare it on the in-memory Table + Mapper so ORM queries
    can reference it.

    Idempotent — if the class already has `tenant_id` (because some
    future canonical model adds it declaratively), this is a no-op.
    """
    try:
        mapper = inspect(cls)
    except Exception as e:
        logger.warning("tenant_filter: cannot inspect %s: %s", cls, e)
        return

    # Already declared (canonical model added it) — nothing to do.
    if 'tenant_id' in mapper.columns:
        return

    # Add the column to the Table object. append_column is in-memory
    # only; SQLAlchemy never re-issues DDL for it.
    table = cls.__table__
    if 'tenant_id' not in table.c:
        try:
            table.append_column(
                Column('tenant_id', String(64), nullable=True, index=True))
        except Exception as e:
            logger.warning("tenant_filter: append_column failed for %s: %s",
                           cls, e)
            return

    # Register the column as a mapped property so cls.tenant_id works.
    try:
        mapper.add_property('tenant_id', table.c.tenant_id)
    except Exception as e:
        logger.warning("tenant_filter: add_property failed for %s: %s",
                       cls, e)


def register_tenant_aware(cls: Type) -> None:
    """Mark `cls` as tenant-aware: queries are filtered, inserts stamped.

    Safe to call before or after `install_tenant_filter()`.  Idempotent.
    """
    with _TENANT_AWARE_LOCK:
        if cls in _TENANT_AWARE:
            return
        _augment_class(cls)
        _TENANT_AWARE.append(cls)
    logger.debug("tenant_filter: registered %s", cls.__name__)


def get_tenant_aware_classes() -> List[Type]:
    """Snapshot of the registered tenant-aware classes (test/inspection)."""
    with _TENANT_AWARE_LOCK:
        return list(_TENANT_AWARE)


def _current_tenant_id():
    """Return the current request's tenant_id, or None if not in a Flask
    request context. Wrapped in try/except to gracefully handle:

      - Flask not installed (extreme degraded boot)
      - Outside any request (daemon threads, scripts, tests)
      - g.tenant_id not set (require_auth never ran — admin-only routes
        bypassing auth, or pre-7a code path)
    """
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        return getattr(g, 'tenant_id', None)
    except Exception:
        return None


def _strict_mode_enabled() -> bool:
    """Phase 8 — when `tenant_strict_mode` flag is on, the NULL
    pass-through is dropped: legacy untenanted rows become invisible
    to tenanted requests.  This is the Pass-2 H-NEW-2 hardening
    promised at the time of the original loose-mode trade-off, plus
    the closely related Pass-4 P4-3 system-agent concern (legacy
    rows could carry across tenants).

    Falls back to env var `HEVOLVE_FLAG_TENANT_STRICT_MODE` when no
    Flask context is active so tests + daemon threads can opt in.

    Priority order (Pass-5 F6 — same shape as feature_flags.get_flag,
    just compressed because g.feature_flags has already resolved
    tenant > env > default at request boot):

      1. g.feature_flags['tenant_strict_mode']  (per-request decided)
      2. HEVOLVE_FLAG_TENANT_STRICT_MODE env var (PROCESS-GLOBAL —
         see module docstring caveat; do not set on multi-tenant
         cloud nodes unless every tenant should be strict)
      3. False (the default-OFF default)
    """
    try:
        from flask import g, has_request_context
        if has_request_context():
            flags = getattr(g, 'feature_flags', None) or {}
            if 'tenant_strict_mode' in flags:
                return bool(flags['tenant_strict_mode'])
    except Exception:
        pass
    # Env-var fallback so tests + scripts can flip the mode without
    # bootstrapping a full Flask request.
    import os
    raw = os.environ.get('HEVOLVE_FLAG_TENANT_STRICT_MODE', '')
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _build_criterion(cls, tid, strict):
    """Build a `with_loader_criteria` option for `cls`.

    Two modes:
      Loose  → `(tenant_id == :tid) OR (tenant_id IS NULL)`
      Strict → `(tenant_id == :tid)` (NULL rows hidden)

    Critical implementation detail: SQLAlchemy 2.0 tracks lambda
    CLOSURE variables for cache-invalidation, but NOT default-args
    (`__defaults__`).  Using `lambda c, _tid=tid: ...` would bake
    the FIRST call's tid into the cached SQL forever; subsequent
    calls with different tids would reuse the stale SQL.  The fix
    is to capture `tid` via ACTUAL closure (no default arg) so
    SQLAlchemy's tracker correctly invalidates per-tid.

    Strict vs loose is encoded by RETURNING two structurally-
    different lambdas (different `__code__`), so the cache
    naturally separates them.
    """
    if strict:
        def strict_criterion(c):
            return c.tenant_id == tid
        return with_loader_criteria(
            cls, strict_criterion, include_aliases=True)
    else:
        def loose_criterion(c):
            return or_(c.tenant_id == tid, c.tenant_id.is_(None))
        return with_loader_criteria(
            cls, loose_criterion, include_aliases=True)


def install_tenant_filter() -> None:
    """Install do_orm_execute + before_flush listeners on Session.

    Idempotent — repeat calls are a no-op. Listeners only fire when
    inside a Flask request context AND `g.tenant_id` is non-None,
    making the install free of regression for single-tenant deploys.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True

    @event.listens_for(Session, 'do_orm_execute')
    def _on_orm_execute(execute_state):
        # Only filter SELECTs — INSERTs/UPDATEs/DELETEs use before_flush.
        if not execute_state.is_select:
            return
        tid = _current_tenant_id()
        if tid is None:
            return  # flat/regional or outside request context — pass-through

        # Snapshot of registered classes — caller may register more
        # after install, and this list is rebuilt every query so they
        # pick up automatically.
        with _TENANT_AWARE_LOCK:
            classes = list(_TENANT_AWARE)

        # Phase 8 — strict mode drops the NULL pass-through.  Legacy
        # untenanted rows become invisible to tenanted requests.
        # Tradeoff vs. loose mode (default): strict gives stronger
        # cross-tenant isolation guarantees at the cost of breaking
        # backward-compat reads of pre-v40 rows.  Choose per tenant.
        #
        # IMPLEMENTATION NOTE — lambda caching gotcha:
        # SQLAlchemy's `with_loader_criteria` caches compiled SQL by
        # the lambda's CODE object.  Earlier attempts used two
        # different lambda bodies (one for strict, one for loose),
        # which produced cache pollution where the wrong-mode lambda
        # was reused under sequential test execution.
        #
        # The fix: ALWAYS use the SAME lambda body that includes
        # BOTH branches as SQL clauses.  Strict mode is encoded as a
        # plain Python conditional INSIDE the lambda where the
        # `_strict` value is captured via default-arg.  SQLAlchemy
        # tracks `_tid` as a closure variable but treats the default-
        # arg branch decision as part of the SQL shape — so each
        # call's actual SQL output matches the captured strict value.
        # The cache may store one entry per (code, _strict-arg-value)
        # pair which is what we want.
        strict = _strict_mode_enabled()

        for cls in classes:
            execute_state.statement = execute_state.statement.options(
                _build_criterion(cls, tid, strict)
            )

    @event.listens_for(Session, 'before_flush')
    def _on_before_flush(session, flush_context, instances):
        tid = _current_tenant_id()
        if tid is None:
            return

        with _TENANT_AWARE_LOCK:
            classes = tuple(_TENANT_AWARE)

        if not classes:
            return

        # Auto-stamp tenant_id on new (INSERT) instances of registered
        # classes. Never overwrites an explicit value the caller set —
        # this is purely a default for code that pre-dates 7a.
        for instance in session.new:
            if isinstance(instance, classes):
                if getattr(instance, 'tenant_id', None) is None:
                    try:
                        instance.tenant_id = tid
                    except Exception:
                        pass

    logger.info("tenant_filter: installed (do_orm_execute + before_flush "
                "listeners on global Session)")


__all__ = [
    'install_tenant_filter',
    'register_tenant_aware',
    'get_tenant_aware_classes',
]
