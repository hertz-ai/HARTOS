"""Phase 8.B — WAMP per-tenant subscribe ACL (#265).

Plan reference: sunny-gliding-eich.md, Part E.13 + Part 8.

Covers:
  - Public topics → allow for any authenticated user.
  - tenant.<tid>.* → allow only when JWT tid matches.
  - tenant.<tid>.user.<uid>.* → allow only when JWT user_id matches.
  - Cross-tenant snoop refused (the headline isolation guarantee).
  - Segment-match (not substring) — same Pass-2 N-NEW-4 lesson.
  - Malformed input → fail closed.
  - resolve_tenant_slug returns None for unknown slug or missing
    tenants table (pre-Phase-8 deploy).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── authorize_subscribe ────────────────────────────────────────────

def test_public_topic_allowed_for_authenticated_user():
    from integrations.social.tenant_acl import authorize_subscribe
    assert authorize_subscribe(
        'community.feed', {'user_id': 'u-1'}) is True
    assert authorize_subscribe(
        'chat.social', {'user_id': 'u-1'}) is True


def test_public_topic_refused_for_anonymous():
    from integrations.social.tenant_acl import authorize_subscribe
    # No user_id in JWT → refuse even on public topic.
    assert authorize_subscribe('community.feed', {}) is False
    assert authorize_subscribe('community.feed', None) is False


def test_tenant_topic_allowed_when_tid_matches():
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'u-1', 'tid': 'tenant-A'}
    assert authorize_subscribe(
        'tenant.tenant-A.community.c-1.message', payload) is True
    assert authorize_subscribe(
        'tenant.tenant-A.conv.c-1.message', payload) is True


def test_cross_tenant_snoop_refused():
    """Headline guarantee: tenant-A user cannot subscribe to
    tenant-B topics, period."""
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'u-1', 'tid': 'tenant-A'}
    assert authorize_subscribe(
        'tenant.tenant-B.user.u-1.message', payload) is False
    assert authorize_subscribe(
        'tenant.tenant-B.conv.c-1.message', payload) is False
    assert authorize_subscribe(
        'tenant.tenant-B.community.c-1.message', payload) is False


def test_user_scoped_topic_must_match_jwt_user_id():
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'alice', 'tid': 'tenant-A'}
    # Alice subscribing to her own inbox → allow
    assert authorize_subscribe(
        'tenant.tenant-A.user.alice.message', payload) is True
    # Alice subscribing to bob's inbox → refuse (within same tenant)
    assert authorize_subscribe(
        'tenant.tenant-A.user.bob.message', payload) is False


def test_segment_match_not_substring_match():
    """Pass-2 N-NEW-4 attack vector applied to subscribe authorizer:
    `tenant.x.attack.fake.user.alice.message` must NOT count as
    a user-scope match for alice just because 'user.alice.' appears
    later in the topic."""
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'alice', 'tid': 'tenant-A'}
    # The third segment is 'attack', not 'user' or 'conv' or
    # 'community' — refuse.
    assert authorize_subscribe(
        'tenant.tenant-A.attack.fake.user.alice.message', payload) is False


def test_unknown_scope_refused():
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'u-1', 'tid': 'tenant-A'}
    assert authorize_subscribe(
        'tenant.tenant-A.weird-scope.x', payload) is False


def test_malformed_input_fails_closed():
    """Empty topic, missing payload, garbage shape — all refuse."""
    from integrations.social.tenant_acl import authorize_subscribe
    assert authorize_subscribe('', {'user_id': 'u-1'}) is False
    assert authorize_subscribe(None, {'user_id': 'u-1'}) is False
    assert authorize_subscribe('tenant.', {'user_id': 'u-1'}) is False


def test_legacy_per_user_topic_suffix_match():
    """`com.hertzai.hevolve.social.<user_id>` legacy topic shape —
    allow when suffix matches the JWT user_id."""
    from integrations.social.tenant_acl import authorize_subscribe
    payload = {'user_id': 'alice'}
    assert authorize_subscribe(
        'com.hertzai.hevolve.social.alice', payload) is True
    assert authorize_subscribe(
        'com.hertzai.hevolve.social.bob', payload) is False


# ── resolve_tenant_slug ────────────────────────────────────────────

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
    db = get_db()
    try:
        yield db
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


def test_resolve_tenant_slug_returns_none_when_table_missing(fresh_db):
    """Pre-Phase-8 deploys have no `tenants` table; resolver
    degrades to None instead of raising."""
    from integrations.social.tenant_acl import resolve_tenant_slug
    db = fresh_db
    assert resolve_tenant_slug(db, 'unknown-slug') is None


def test_resolve_tenant_slug_returns_row_when_present(fresh_db):
    """When the tenants table exists and has a matching row,
    return its dict shape."""
    from sqlalchemy import text
    from integrations.social.tenant_acl import resolve_tenant_slug
    db = fresh_db
    # Create the tenants table on demand (Phase 8 migration is
    # not in this suite's v51 yet — fixture covers the table
    # existence assertion).
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS tenants ("
        "id VARCHAR(64) PRIMARY KEY, "
        "name VARCHAR(200), slug VARCHAR(80) UNIQUE, "
        "plan VARCHAR(20), is_suspended INTEGER DEFAULT 0)"))
    tid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO tenants (id, name, slug, plan) "
        "VALUES (:id, 'Acme Corp', 'acme-corp', 'pro')"),
        {'id': tid})
    db.commit()
    row = resolve_tenant_slug(db, 'acme-corp')
    assert row is not None
    assert row['id'] == tid
    assert row['slug'] == 'acme-corp'
    assert row['plan'] == 'pro'
    assert row['is_suspended'] is False
    # Unknown slug → None
    assert resolve_tenant_slug(db, 'no-such-slug') is None


def test_resolve_tenant_slug_marks_suspended(fresh_db):
    from sqlalchemy import text
    from integrations.social.tenant_acl import resolve_tenant_slug
    db = fresh_db
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS tenants ("
        "id VARCHAR(64) PRIMARY KEY, "
        "name VARCHAR(200), slug VARCHAR(80) UNIQUE, "
        "plan VARCHAR(20), is_suspended INTEGER DEFAULT 0)"))
    db.execute(text(
        "INSERT INTO tenants (id, name, slug, plan, is_suspended) "
        "VALUES (:id, 'Suspended', 'gone', 'free', 1)"),
        {'id': str(uuid.uuid4())})
    db.commit()
    row = resolve_tenant_slug(db, 'gone')
    assert row['is_suspended'] is True
