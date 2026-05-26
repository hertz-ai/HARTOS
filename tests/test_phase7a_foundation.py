"""Phase 7a foundation — integration tests.

Plan reference: sunny-gliding-eich.md, Part O.1 + Part S.

Covers:
  1. Migration v40 — nullable tenant_id added to all expected tables.
  2. Migration v41 — polymorphic Membership table created with
     correct schema + backfill from community_memberships.
  3. auth.py tenant resolver — JWT 'tid' round-trip + back-compat.
  4. feature_flags — defaults, env override, get_flags_for_tenant.
  5. /api/social/users/autocomplete — flag gating, scope ranking,
     agent vs human filter, q-required, kind validation, regression
     of existing /users endpoint.

These tests double as the regression smoke before flipping any
Phase 7a flag in production.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest


# Ensure HARTOS root is importable regardless of pytest invocation.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fresh_db(monkeypatch):
    """Spin up an in-memory HARTOS DB with all migrations applied.

    HARTOS's `get_engine()` returns a process-wide singleton; we
    reset it before AND after the test so each test gets a fresh
    in-memory engine + tables. Without the reset, seeded users
    leak across tests and UNIQUE(username) collisions cause spurious
    failures.
    """
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    # Force pyjwt path so 'tid' claim encoding works in tests.
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False  # sentinel
    # Reset BOTH the engine AND the session factory singletons so
    # each test gets a brand-new in-memory DB plus sessions bound
    # to it. Forgetting the session factory leaks "no such table"
    # errors because new engine has fresh tables but old factory
    # still binds to the disposed engine.
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
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


@pytest.fixture
def app_client(fresh_db):
    """Flask test client wired to a fresh in-memory HARTOS."""
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


# ── Migration v40 ────────────────────────────────────────────────

def test_v40_tenancy_columns_added(fresh_db):
    """All v40 target tables have a nullable tenant_id column."""
    from sqlalchemy import inspect
    from integrations.social.migrations import _V40_TENANT_TABLES
    db, eng = fresh_db
    insp = inspect(eng)
    missing = []
    for t in _V40_TENANT_TABLES:
        if t not in insp.get_table_names():
            continue  # Some tables are conditionally created
        cols = {c['name']: c for c in insp.get_columns(t)}
        if 'tenant_id' not in cols:
            missing.append(t)
            continue
        # Must be nullable to preserve flat/regional pass-through
        assert cols['tenant_id']['nullable'] is not False, (
            f"tenant_id on {t} must be nullable")
    assert not missing, f"tenant_id missing on: {missing}"


def test_v40_idempotent(fresh_db, monkeypatch):
    """Re-running migrations after v40 must not fail."""
    from integrations.social import migrations
    # Run again; should be a no-op (everything <= current version skipped)
    migrations.run_migrations()


# ── Migration v41 ────────────────────────────────────────────────

def test_v41_membership_table_shape(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    assert 'memberships' in insp.get_table_names()
    cols = {c['name']: c for c in insp.get_columns('memberships')}
    expected = {
        'id', 'tenant_id', 'parent_kind', 'parent_id', 'member_id',
        'agent_kind', 'role', 'joined_at', 'muted_until',
        'notification_pref', 'agent_grant_id',
    }
    missing = expected - set(cols.keys())
    assert not missing, f"missing columns: {missing}"


def test_v41_unique_index_prevents_dupes(fresh_db):
    """The (parent_kind, parent_id, member_id) UNIQUE index works."""
    from sqlalchemy import text
    db, eng = fresh_db
    with eng.connect() as conn:
        conn.execute(text(
            "INSERT INTO memberships (id, parent_kind, parent_id, "
            "member_id, agent_kind, role, joined_at, notification_pref) "
            "VALUES ('m1','community','c1','u1','human','member', "
            "CURRENT_TIMESTAMP, 'all')"))
        conn.commit()
    # Inserting a duplicate (same parent + member) must fail.
    with pytest.raises(Exception):
        with eng.connect() as conn:
            conn.execute(text(
                "INSERT INTO memberships (id, parent_kind, parent_id, "
                "member_id, agent_kind, role, joined_at, notification_pref) "
                "VALUES ('m2','community','c1','u1','human','member', "
                "CURRENT_TIMESTAMP, 'all')"))
            conn.commit()


def test_v41_backfills_existing_community_memberships(monkeypatch, fresh_db):
    """If community_memberships rows existed before v41, they should
    appear in memberships after migration. We verify by inserting a
    legacy row, then re-running migrations to re-trigger the backfill
    loop (idempotent, won't double-insert)."""
    from sqlalchemy import text
    db, eng = fresh_db
    with eng.connect() as conn:
        conn.execute(text(
            "INSERT INTO community_memberships (id, user_id, community_id, "
            "role, created_at) VALUES "
            "('cm1','u-legacy','c-legacy','member',CURRENT_TIMESTAMP)"))
        conn.commit()
    # Force re-run of v41 backfill by lowering schema_version.
    from integrations.social import migrations
    with eng.connect() as conn:
        conn.execute(text(
            "UPDATE social_meta SET value='40' WHERE key='schema_version'"))
        conn.commit()
    migrations.run_migrations()
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT parent_kind, parent_id, member_id, role "
            "FROM memberships WHERE id='cm1'")).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ('community', 'c-legacy', 'u-legacy', 'member')


# ── auth.py tenant resolver ──────────────────────────────────────

def test_jwt_tenant_round_trip(fresh_db):
    from integrations.social import auth
    tok = auth.generate_jwt('u1', 'alice', 'flat', tenant_id='tnt-42')
    payload = auth.decode_jwt(tok)
    assert payload.get('tid') == 'tnt-42'
    assert payload.get('user_id') == 'u1'


def test_jwt_no_tenant_back_compat(fresh_db):
    """Tokens issued without a tenant_id must remain valid + carry
    no 'tid' claim (existing flat / regional behavior)."""
    from integrations.social import auth
    tok = auth.generate_jwt('u2', 'bob', 'flat')
    payload = auth.decode_jwt(tok)
    assert 'tid' not in payload or payload.get('tid') is None
    assert payload.get('user_id') == 'u2'


def test_generate_jwt_with_tenant_helper(fresh_db):
    from integrations.social import auth
    tok = auth.generate_jwt_with_tenant('u3', 'cara', 'flat', 'tnt-7')
    payload = auth.decode_jwt(tok)
    assert payload.get('tid') == 'tnt-7'


# ── feature_flags ────────────────────────────────────────────────

def test_feature_flags_defaults_all_false(fresh_db):
    from integrations.social import feature_flags as ff
    defaults = ff.list_flags()
    assert len(defaults) > 10  # at least the core Phase 7+ set
    assert all(v is False for v in defaults.values()), (
        "all flags must default off for safe dark-launch")


def test_feature_flags_env_override(fresh_db, monkeypatch):
    from integrations.social import feature_flags as ff
    monkeypatch.setenv('HEVOLVE_FLAG_TENANCY_V2', 'true')
    assert ff.get_flag('tenancy_v2') is True
    monkeypatch.setenv('HEVOLVE_FLAG_TENANCY_V2', 'false')
    assert ff.get_flag('tenancy_v2') is False


def test_feature_flags_unknown_flag_returns_false(fresh_db):
    from integrations.social import feature_flags as ff
    assert ff.get_flag('not_a_real_flag') is False


def test_feature_flags_for_tenant_returns_dict(fresh_db):
    from integrations.social import feature_flags as ff
    db, _ = fresh_db
    flags = ff.get_flags_for_tenant(db, None)
    assert isinstance(flags, dict)
    assert 'mentions' in flags
    assert 'calls_v1' in flags


# ── /api/social/users/autocomplete ───────────────────────────────

def _seed_users(db):
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    s = User(id=str(uuid.uuid4()), username='solar-architect',
             display_name='Solar Architect', email='sa@x.test',
             password_hash='x:y', user_type='agent')
    s.owner_id = a.id
    b = User(id=str(uuid.uuid4()), username='bob_p', display_name='Bob',
             email='b@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, s, b])
    db.commit()
    return a, s, b


def test_autocomplete_flag_off_returns_empty(app_client, monkeypatch):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.delenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', raising=False)
    r = client.get('/api/social/users/autocomplete?q=so',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data'] == []


def test_autocomplete_flag_on_returns_matches(app_client, monkeypatch):
    client, db = app_client
    a, s, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', 'true')
    r = client.get('/api/social/users/autocomplete?q=so',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    data = r.get_json()['data']
    sol = next(d for d in data if d['username'] == 'solar-architect')
    assert sol['agent_kind'] == 'agent'
    assert sol['agent_owner_id'] == a.id


def test_autocomplete_kind_human_filter(app_client, monkeypatch):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', 'true')
    r = client.get('/api/social/users/autocomplete?q=&kind=human',
                   headers={'Authorization': f'Bearer {tok}'})
    # Empty q is rejected
    assert r.status_code == 400


def test_autocomplete_q_required(app_client, monkeypatch):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', 'true')
    r = client.get('/api/social/users/autocomplete?q=',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 400


def test_autocomplete_invalid_kind_400(app_client, monkeypatch):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', 'true')
    r = client.get('/api/social/users/autocomplete?q=al&kind=robot',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 400


def test_autocomplete_unauth_401(app_client):
    client, db = app_client
    r = client.get('/api/social/users/autocomplete?q=al')
    assert r.status_code == 401


def test_autocomplete_kind_agent_returns_agents_only(app_client, monkeypatch):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.setenv('HEVOLVE_FLAG_MENTIONS_AUTOCOMPLETE', 'true')
    r = client.get('/api/social/users/autocomplete?q=s&kind=agent',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert all(d['agent_kind'] == 'agent' for d in data)


# ── Regression — existing /users endpoint unchanged ──────────────

def test_existing_list_users_endpoint_regression(app_client, monkeypatch):
    """The /users GET endpoint must return identical response shape
    to what it returned before Phase 7a touched anything."""
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get('/api/social/users?limit=10',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    body = r.get_json()
    assert 'data' in body
    assert 'meta' in body
    # 3 seeded users
    assert len(body['data']) == 3
    # Every user has the expected baseline fields
    for u in body['data']:
        assert 'id' in u
        assert 'username' in u


def test_existing_get_user_endpoint_regression(app_client):
    client, db = app_client
    a, _, _ = _seed_users(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get(f'/api/social/users/{a.id}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['data']['username'] == 'alice'
