"""Phase 8 — tenant_filter strict mode (NULL pass-through hardening).

Plan reference: sunny-gliding-eich.md, Part 8 + Pass-2 H-NEW-2 deferred + Pass-4 P4-3 fallout.

The default tenant_filter listener applies:
    (col == :tid) OR (col IS NULL)
to every SELECT — the NULL pass-through preserves backward compat
for legacy untenanted rows from before migration v40.  In strict
mode (Phase 8 hardening), the NULL arm is dropped:
    (col == :tid)
so legacy rows are invisible to tenanted requests.  This closes
the cross-tenant leak surface for tenants that have completed the
v40 backfill.

Coverage:
  - Loose (default): tenant-A user sees own + NULL rows.
  - Strict: tenant-A user sees own ONLY; NULL rows hidden.
  - Strict: cross-tenant leak still impossible (tenant-A → tenant-B
    rows hidden, identical to loose).
  - Mode toggle takes effect per-request (not at install time).

Note: the existing test_phase7_tenant_filter.py covers loose-mode
behavior; this file is the strict-mode counterpart.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_db_with_tenant_filter(monkeypatch):
    """Per-test in-memory HARTOS DB with the tenant_filter listener
    installed against User + Post.  Mirrors what init_social does
    in production but without the full blueprint zoo."""
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db, User, Post
    from integrations.social.tenant_filter import (
        install_tenant_filter, register_tenant_aware,
    )
    eng = get_engine()
    migrations.run_migrations()
    install_tenant_filter()
    for cls in (User, Post):
        register_tenant_aware(cls)
    db = get_db()
    try:
        yield db, eng
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


def _seed_users(db, n=2):
    from integrations.social.models import User
    users = []
    for i in range(n):
        u = User(id=str(uuid.uuid4()),
                 username=f'u{i}_{uuid.uuid4().hex[:6]}',
                 display_name=f'U{i}',
                 email=f'u{i}_{uuid.uuid4().hex[:6]}@x.test',
                 password_hash='x:y', user_type='human')
        users.append(u)
    db.add_all(users)
    db.commit()
    return users


def _stamp_tenant(db, table, row_id, tenant_id):
    from sqlalchemy import text
    db.execute(text(f"UPDATE {table} SET tenant_id = :t WHERE id = :id"),
               {'t': tenant_id, 'id': row_id})
    db.commit()


def _make_app():
    """Bare Flask app — we just need a request context to set
    g.tenant_id + g.feature_flags so the listener fires."""
    from flask import Flask
    return Flask(__name__)


# ── Loose mode (default): NULL pass-through ───────────────────────

def test_loose_mode_sees_null_rows(fresh_db_with_tenant_filter, monkeypatch):
    """Default (no flag set): legacy NULL-tenant rows ARE visible to
    tenanted requests.  This is the documented backward-compat
    invariant from Phase 7a — locked here as a baseline."""
    monkeypatch.delenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', raising=False)
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')
    # b's tenant_id stays NULL (legacy row)

    from integrations.social.models import User
    app = _make_app()
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {}  # tenant_strict_mode unset → loose
        rows = db.query(User).all()
        ids = {u.id for u in rows}
        assert a.id in ids   # own tenant
        assert b.id in ids   # NULL legacy row passes through


# ── Strict mode: NULL rows hidden ─────────────────────────────────

def test_strict_mode_hides_null_rows(fresh_db_with_tenant_filter,
                                     monkeypatch):
    """tenant_strict_mode flag on → NULL pass-through dropped."""
    monkeypatch.delenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', raising=False)
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')
    # b's tenant_id stays NULL

    from integrations.social.models import User
    app = _make_app()
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {'tenant_strict_mode': True}
        rows = db.query(User).all()
        ids = {u.id for u in rows}
        assert a.id in ids
        assert b.id not in ids, (
            "strict mode must hide legacy NULL rows from tenanted "
            "requests — backward-compat trade-off is opt-in")


def test_strict_mode_via_env_var(fresh_db_with_tenant_filter, monkeypatch):
    """Env-var fallback so daemon threads / scripts can opt in
    without bootstrapping a Flask request."""
    monkeypatch.setenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', 'true')
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')

    from integrations.social.models import User
    app = _make_app()
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        # No g.feature_flags set — env-var fallback drives strict mode.
        rows = db.query(User).all()
        ids = {u.id for u in rows}
        assert a.id in ids
        assert b.id not in ids


def test_strict_mode_still_blocks_cross_tenant(
        fresh_db_with_tenant_filter, monkeypatch):
    """Strict mode must NOT regress cross-tenant isolation — a
    tenant-B row stays invisible to a tenant-A user, exactly as in
    loose mode.  This is the headline isolation guarantee."""
    monkeypatch.delenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', raising=False)
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')
    _stamp_tenant(db, 'users', b.id, 'tenant-B')

    from integrations.social.models import User
    app = _make_app()
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {'tenant_strict_mode': True}
        rows = db.query(User).all()
        ids = {u.id for u in rows}
        assert a.id in ids
        assert b.id not in ids   # cross-tenant always blocked


def test_strict_mode_per_request_toggle(
        fresh_db_with_tenant_filter, monkeypatch):
    """The mode is read from g.feature_flags AT QUERY TIME, not at
    install time.  Two requests in the same process can have
    different modes — one tenant migrating to strict while another
    stays on loose."""
    monkeypatch.delenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', raising=False)
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')

    from integrations.social.models import User
    app = _make_app()

    # Request 1: loose — sees NULL rows
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {'tenant_strict_mode': False}
        loose_count = db.query(User).count()
    # Request 2: strict — does NOT see NULL rows
    with app.test_request_context():
        from flask import g
        g.tenant_id = 'tenant-A'
        g.feature_flags = {'tenant_strict_mode': True}
        strict_count = db.query(User).count()

    assert loose_count > strict_count, (
        f"strict ({strict_count}) must hide rows that loose "
        f"({loose_count}) shows — but counts matched")


def test_strict_mode_no_op_when_no_tenant(
        fresh_db_with_tenant_filter, monkeypatch):
    """Outside a tenant context (g.tenant_id is None), strict mode
    has nothing to enforce — the listener no-ops just like loose
    mode does.  This is the flat / regional / Nunba bundled
    invariant: enabling strict mode in code never affects single-
    tenant deploys."""
    monkeypatch.setenv('HEVOLVE_FLAG_TENANT_STRICT_MODE', 'true')
    db, _ = fresh_db_with_tenant_filter
    a, b = _seed_users(db, 2)
    _stamp_tenant(db, 'users', a.id, 'tenant-A')
    # b's tenant_id is NULL

    from integrations.social.models import User
    # No request context at all — listener returns early
    rows = db.query(User).all()
    ids = {u.id for u in rows}
    assert a.id in ids and b.id in ids


# ── Pass-4 P4-15: human_overrule not-found check ──────────────────

def test_human_overrule_not_found_raises(fresh_db_with_tenant_filter):
    """Pass-4 P4-15: a missing decision_id used to silently UPDATE
    zero rows + return success.  Now raises ValueError so caller
    bugs surface immediately."""
    from integrations.social.content_classifier import ContentClassifier
    db, _ = fresh_db_with_tenant_filter
    with pytest.raises(ValueError) as exc:
        ContentClassifier.human_overrule(
            db, decision_id='does-not-exist',
            reviewer_id='whoever',
            human_decision='allow')
    assert 'not found' in str(exc.value).lower()
