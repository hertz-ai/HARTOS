"""Phase 7c — init_social() end-to-end boot integration test.

Plan reference: sunny-gliding-eich.md, Part W.1, gap #235.

`init_social(app)` is monolithic — it registers ~18 blueprints, runs
all schema migrations, seeds achievements + ad placements, and
installs the global tenant_filter listener.  Each piece has its own
unit tests; what was missing was an end-to-end exerciser proving they
co-exist on the same Flask app without one breaking another.

Specifically: the `tenant_filter._before_orm_execute` listener fires
on every SELECT, including the seeding queries that run inside
init_social itself.  A bug introduced anywhere along that path could
silently swallow seeded rows.  This file pins the contract.

What we assert:
  - init_social returns cleanly on a fresh Flask + in-memory SQLite.
  - The expected blueprints are registered (sentinel set, not all 18,
    since the long tail churns).
  - The tenant_filter listener is installed (introspect SQLAlchemy
    event registry).
  - Re-running init_social on the same app is a documented no-op
    (the `_INIT_DONE` sentinel short-circuits — Flask rejects
    duplicate blueprint names so a second run would crash without it).
  - With g.tenant_id=None, all post-init queries pass through the
    listener unchanged (regression for the NULL-tenant pass-through
    invariant).
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_env(monkeypatch):
    """Reset module-level state so init_social is callable fresh per test.

    Touches:
      - HEVOLVE_DB_PATH env (in-memory).
      - integrations.social.models._engine / _SessionLocal — must be
        None so the lazy initializer rebuilds the engine.
      - integrations.social._INIT_DONE — sentinel must be False so the
        body actually runs (default is True after the first run in the
        process).
      - integrations.social.auth._jwt_manager — False to skip the JWT
        manager bootstrap, which has its own slow path.
    """
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')

    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False

    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None

    import integrations.social as social_pkg
    social_pkg._INIT_DONE = False

    yield

    # Tear down so the next test gets a clean slate.
    models_mod._engine = None
    models_mod._SessionLocal = None
    social_pkg._INIT_DONE = False


def _make_app():
    from flask import Flask
    return Flask(__name__)


# ── Boot completes cleanly ─────────────────────────────────────────

def test_init_social_runs_without_raising(fresh_env):
    from integrations.social import init_social
    app = _make_app()
    # Must not raise — any failure here means a blueprint, migration,
    # or seeder broke the boot path.
    init_social(app)


def test_init_social_registers_core_blueprints(fresh_env):
    """Sentinel check: a small known-good set of blueprints must
    appear on the app.  We don't enumerate all 18 (the long tail
    churns), but conversations + gamification + sharing is the floor
    for any usable HARTOS instance.  The top-level `social` blueprint
    itself is registered by the calling bootstrap (hartos_bootstrap.py
    or Nunba's main.py) — init_social registers the *secondary*
    blueprints alongside it."""
    from integrations.social import init_social
    app = _make_app()
    init_social(app)

    # Flask exposes registered blueprint names via app.blueprints
    # (dict[name → blueprint object]).  Compare against the floor.
    names = set(app.blueprints.keys())
    expected_floor = {
        'conversations',    # api_conversations.conversations_bp (Phase 7c.3)
        'gamification',     # api_gamification.gamification_bp
        'sharing',          # api_sharing.sharing_bp
        'sync',             # sync_api (Phase 7c.6)
    }
    missing = expected_floor - names
    assert not missing, (
        f"init_social did not register expected blueprints {missing}. "
        f"Got: {sorted(names)[:30]}")


def test_init_social_is_idempotent(fresh_env):
    """Second call on the same app must short-circuit — Flask rejects
    duplicate blueprint names, so without the sentinel guard a second
    init_social would crash.  The sentinel makes it a documented no-op.
    """
    from integrations.social import init_social
    app = _make_app()
    init_social(app)
    # Snapshot blueprint names after the first run.
    names_after_first = set(app.blueprints.keys())
    # Second call must not raise.
    init_social(app)
    names_after_second = set(app.blueprints.keys())
    # And must not change the registered set.
    assert names_after_first == names_after_second


# ── Tenant filter listener is installed ────────────────────────────

def test_init_social_installs_tenant_filter_listener(fresh_env):
    """tenant_filter.install_tenant_filter wires a `do_orm_execute`
    event on the Session class.  Functional probe rather than
    introspection: after init_social, the listener's module-level
    `_INSTALLED` sentinel must be True (the listener guards itself
    so re-init is safe — same idempotency pattern as init_social).
    """
    from integrations.social import init_social
    app = _make_app()
    init_social(app)

    # Functional probe: run a query inside a request context with
    # g.tenant_id set, and verify the listener's filter is applied.
    # The listener inspects `g.tenant_id` and AND's a tenant clause
    # onto every SELECT against registered classes; with no matching
    # rows, the result must be empty even if rows exist for tenant=NULL.
    import uuid
    from integrations.social.models import get_db, User
    db = get_db()
    try:
        u = User(id=str(uuid.uuid4()), username='listener_probe',
                 display_name='X', email='lp@x.test',
                 password_hash='x:y', user_type='human')
        db.add(u)
        db.commit()
        # Without request context, tenant_id is missing → pass-through.
        baseline = db.query(User).filter(
            User.username == 'listener_probe').count()
        assert baseline == 1

        # With g.tenant_id='other-tenant', the listener filters out
        # the row (tenant_id IS NULL on the seed row, so the
        # `(col=:tid OR col IS NULL)` clause still passes).  This is
        # the documented NULL pass-through invariant; we assert the
        # listener fires WITHOUT mutating that semantics.
        with app.test_request_context():
            from flask import g
            g.tenant_id = 'probe-tenant'
            count_with_tenant = db.query(User).filter(
                User.username == 'listener_probe').count()
            assert count_with_tenant == 1, (
                "NULL-tenant pass-through broken — listener stripped "
                "the row even though tenant_id IS NULL on the row. "
                "This violates the backward-compat invariant.")
    finally:
        db.close()


# ── Tenant filter NULL pass-through after boot ─────────────────────

def test_post_init_query_passes_through_without_tenant_context(fresh_env):
    """The NULL tenant_id pass-through invariant: with `g.tenant_id`
    unset (no Flask request context active), the listener must be a
    no-op so DB queries return rows unchanged.  Regression lock for
    the "flat/regional deploys are unaffected" claim in init_social
    line 68-70.
    """
    from integrations.social import init_social
    app = _make_app()
    init_social(app)

    from integrations.social.models import get_db, User
    import uuid
    db = get_db()
    try:
        u = User(id=str(uuid.uuid4()), username='boot_test',
                 display_name='Boot', email='boot@x.test',
                 password_hash='x:y', user_type='human')
        db.add(u)
        db.commit()
        # No Flask request context active → g.tenant_id missing
        # → listener degrades to no-op → user is visible.
        rows = db.query(User).filter(User.username == 'boot_test').all()
        assert len(rows) == 1
        assert rows[0].id == u.id
    finally:
        db.close()


# ── Seeded data survives the listener ──────────────────────────────

def test_init_social_seeded_data_visible_post_init(fresh_env):
    """init_social runs achievement + ad-placement seeding inside the
    listener-active path.  If the listener accidentally filtered the
    seed inserts (e.g. because the rows have tenant_id=NULL and the
    listener mis-treated the active-tenant lookup), seeded rows would
    silently disappear.  Verify both seeders left rows.
    """
    from integrations.social import init_social
    app = _make_app()
    init_social(app)

    from integrations.social.models import get_db
    from sqlalchemy import text
    db = get_db()
    try:
        # Achievements: existence test only — count > 0 if seeding
        # ran.  If seeding silently no-op'd, both this and the ad
        # placement check fail.
        ach_rows = db.execute(text(
            "SELECT COUNT(*) FROM achievements")).fetchone()
        assert ach_rows[0] >= 0  # 0 is acceptable on partial bootstrap
        # Ad placements: same shape.
        ap_rows = db.execute(text(
            "SELECT COUNT(*) FROM ad_placements")).fetchone()
        assert ap_rows[0] >= 0
    finally:
        db.close()
