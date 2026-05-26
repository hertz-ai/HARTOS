"""Phase 7a / Phase 8 — global SQLAlchemy tenant filter integration tests.

Plan reference: sunny-gliding-eich.md, Part C.1 + Part E.1.

Locks the contract for `integrations/social/tenant_filter.py`:

  1. Outside a Flask request context, the listener is a no-op — every
     ORM query returns every row regardless of tenant_id. This is the
     regression-safety property: scripts, daemon threads, and existing
     tests are unaffected by the listener's mere existence.

  2. Inside a request context with `g.tenant_id = None`, the listener
     is also a no-op — flat/regional deploys pass-through unchanged.

  3. Inside a request context with `g.tenant_id = 'A'`, queries against
     tenant-aware classes return rows where `tenant_id = 'A'` OR
     `tenant_id IS NULL` (legacy/untenanted rows stay visible).

  4. Cross-tenant access is silently filtered — tenant A's queries
     never see tenant B's rows.

  5. INSERT auto-stamping: a new instance created inside tenant A's
     request context gets `tenant_id='A'` stamped automatically.

  6. The listener doesn't interfere with non-tenant-aware tables.
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
def fresh_db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
    # Install + register so the listener is wired for these tests.
    from integrations.social.tenant_filter import (
        install_tenant_filter, register_tenant_aware,
    )
    install_tenant_filter()
    from integrations.social.models import (
        User, Post, Community, Notification,
    )
    for cls in (User, Post, Community, Notification):
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


def _seed_two_tenants(db):
    """Seed two users in two tenants + a 'legacy' user with NULL tenant_id."""
    from integrations.social.models import User, Community
    from sqlalchemy import text

    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='bob@x.test', password_hash='x:y', user_type='human')
    legacy = User(id=str(uuid.uuid4()), username='legacy', display_name='Legacy',
                  email='legacy@x.test', password_hash='x:y',
                  user_type='human')
    db.add_all([a, b, legacy])
    db.flush()
    # Stamp tenant_id via raw SQL so we don't rely on the auto-stamp
    # before_flush listener for setup (we test that separately).
    db.execute(text("UPDATE users SET tenant_id = 'A' WHERE id = :id"),
               {'id': a.id})
    db.execute(text("UPDATE users SET tenant_id = 'B' WHERE id = :id"),
               {'id': b.id})
    db.execute(text("UPDATE users SET tenant_id = NULL WHERE id = :id"),
               {'id': legacy.id})
    db.commit()
    return a, b, legacy


# ── 1. No request context → listener is a no-op (regression safety) ──

def test_listener_no_op_outside_request_context(fresh_db):
    """Existing tests and daemon threads run outside Flask request
    context. The listener must not filter — every query returns every
    row. Without this property, every existing test would break."""
    db, _ = fresh_db
    a, b, legacy = _seed_two_tenants(db)
    from integrations.social.models import User
    rows = db.query(User).all()
    usernames = {u.username for u in rows}
    assert {'alice', 'bob', 'legacy'}.issubset(usernames), (
        "outside request context the filter must be inert — got: "
        f"{usernames}")


# ── 2. Request context but g.tenant_id=None → still pass-through ──

def test_listener_no_op_when_tenant_id_none(fresh_db):
    """Flat/regional deploys never set g.tenant_id. The listener
    detects this and falls back to no-filter — the most common path
    in production today must be unchanged."""
    db, _ = fresh_db
    a, b, legacy = _seed_two_tenants(db)

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = None
        from integrations.social.models import User
        rows = db.query(User).all()
        assert {'alice', 'bob', 'legacy'} <= {u.username for u in rows}


# ── 3. Tenant A's request only sees tenant A + NULL rows ──

def test_listener_filters_to_current_tenant(fresh_db):
    """Inside tenant A's request, queries return only tenant A's rows
    plus untenanted (NULL) legacy rows — never tenant B's."""
    db, _ = fresh_db
    a, b, legacy = _seed_two_tenants(db)

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = 'A'
        from integrations.social.models import User
        rows = db.query(User).all()
        usernames = {u.username for u in rows}
        assert 'alice' in usernames, "tenant A must see its own rows"
        assert 'legacy' in usernames, "NULL tenant_id must pass-through"
        assert 'bob' not in usernames, (
            f"tenant A must NOT see tenant B's rows — got: {usernames}")


# ── 4. Cross-tenant by id lookup also blocked ──

def test_listener_blocks_cross_tenant_pk_lookup(fresh_db):
    """Even a direct primary-key lookup is filtered — tenant A's
    request asking for tenant B's user.id returns None, not bob.
    This is the property that prevents IDOR-style cross-tenant access."""
    db, _ = fresh_db
    a, b, legacy = _seed_two_tenants(db)

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = 'A'
        from integrations.social.models import User
        # Tenant A asks for tenant B's user by PK.
        result = db.query(User).filter(User.id == b.id).first()
        assert result is None, (
            "cross-tenant PK lookup must be filtered, got: "
            f"{result.username if result else None}")


# ── 5. INSERT auto-stamping ──

def test_before_flush_auto_stamps_tenant_id(fresh_db):
    """A new User created inside tenant A's request context should be
    stamped with tenant_id='A' automatically — no caller change needed."""
    db, _ = fresh_db

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = 'tenant-A-uuid'
        from integrations.social.models import User
        new_user = User(
            id=str(uuid.uuid4()), username='new_alice',
            display_name='NewAlice', email='na@x.test',
            password_hash='x:y', user_type='human')
        db.add(new_user)
        db.commit()
        # Re-query via raw SQL (escapes the filter) to verify the
        # stamp landed in the row.
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT tenant_id FROM users WHERE id = :id"),
            {'id': new_user.id}).fetchone()
        assert row[0] == 'tenant-A-uuid'


def test_before_flush_does_not_overwrite_explicit_tenant(fresh_db):
    """If the caller explicitly set tenant_id, the auto-stamper must
    not clobber it — caller intent wins."""
    db, _ = fresh_db

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = 'A'
        from integrations.social.models import User
        explicit = User(
            id=str(uuid.uuid4()), username='explicit',
            display_name='Explicit', email='e@x.test',
            password_hash='x:y', user_type='human')
        explicit.tenant_id = 'override-B'  # caller's choice
        db.add(explicit)
        db.commit()
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT tenant_id FROM users WHERE id = :id"),
            {'id': explicit.id}).fetchone()
        assert row[0] == 'override-B'


# ── 6. Non-tenant-aware classes pass through unchanged ──

def test_unregistered_class_not_filtered(fresh_db):
    """The listener only filters classes registered via
    register_tenant_aware. Tables not in that set must not be touched
    — this isolates the blast radius if a class is found to
    misbehave under filtering."""
    db, _ = fresh_db
    a, b, legacy = _seed_two_tenants(db)

    from flask import Flask, g
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.tenant_id = 'A'
        # Achievement is NOT registered as tenant-aware in the
        # fixture, so queries against it ignore tenant entirely.
        from integrations.social.models import Achievement
        # Just verify the query runs without filter errors —
        # achievements is empty in this seed but we want no exception.
        rows = db.query(Achievement).all()
        assert isinstance(rows, list)


# ── 7. Idempotency ──

def test_install_idempotent(fresh_db):
    """install_tenant_filter() must be safe to call multiple times;
    register_tenant_aware() too. Otherwise re-imports during testing or
    hot-reload would multiply listeners."""
    from integrations.social.tenant_filter import (
        install_tenant_filter, register_tenant_aware,
        get_tenant_aware_classes,
    )
    install_tenant_filter()
    install_tenant_filter()  # second call must be a no-op
    from integrations.social.models import User
    register_tenant_aware(User)
    register_tenant_aware(User)  # second call must be a no-op
    classes = get_tenant_aware_classes()
    assert classes.count(User) == 1
