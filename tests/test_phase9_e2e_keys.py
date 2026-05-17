"""Phase 9 — E2E DM key envelope schema (crypto deferred).

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Phase 9 ships the SCHEMA for libsignal-style E2E DMs without the
cryptography itself.  This file locks the contract clients build
against, so the crypto can land later without table churn.

Coverage:
  - Migration v51 creates conversation_keys + message_envelopes
    with the right indexes (UNIQUE active key per (conv, user);
    UNIQUE envelope per (message, recipient)).
  - publish_identity_key idempotent on (conv, user) — re-publishing
    rotates the previous active row (sets rotated_at).
  - rotate_identity_key as standalone soft-rotation.
  - get_active_keys returns only rotated_at IS NULL rows.
  - record_envelope rejects duplicate (message, recipient) pairs.
  - fetch_envelope returns None for missing pairs (no error — the
    member may have joined post-message and have nothing to read).

This is schema-only — no actual crypto invariants are tested.
A follow-up Phase 9 commit adds tests for the actual key
generation, signing, ratcheting, and ciphertext shape.
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


def _seed_conversation(db, creator_id):
    from sqlalchemy import text
    cid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO conversations (id, kind, created_by) "
        "VALUES (:id, 'dm', :cb)"),
        {'id': cid, 'cb': creator_id})
    db.commit()
    return cid


# ── Migration ───────────────────────────────────────────────────────

def test_v51_creates_two_tables(fresh_db):
    from sqlalchemy import text
    db, _ = fresh_db
    for tbl in ('conversation_keys', 'message_envelopes'):
        rows = db.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name = :n"),
            {'n': tbl}).fetchall()
        assert rows, f"v51 did not create table {tbl}"


def test_v51_unique_indexes_present(fresh_db):
    """UNIQUE active key per (conv, user); UNIQUE envelope per
    (message, recipient).  Both are partial unique indexes for the
    active-key case (rotated_at IS NULL)."""
    from sqlalchemy import text
    db, _ = fresh_db
    rows = db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
    names = {r[0] for r in rows}
    assert 'ux_conv_keys_active' in names
    assert 'ux_msg_env_pair' in names


# ── publish_identity_key idempotency ──────────────────────────────

def test_publish_then_republish_rotates_old(fresh_db):
    from sqlalchemy import text
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)

    k1 = E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IK1==')
    k2 = E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IK2==')

    # Two rows total — old one rotated, new one active
    rows = db.execute(text(
        "SELECT id, identity_key_b64, rotated_at FROM conversation_keys "
        "WHERE conversation_id = :cid AND user_id = :uid"),
        {'cid': cid, 'uid': a.id}).fetchall()
    assert len(rows) == 2
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id[k1['id']][1] is not None  # k1 rotated
    assert by_id[k2['id']][1] is None      # k2 active
    # Active key matches the latest publish
    assert by_id[k2['id']][0] == 'IK2=='


def test_publish_requires_identity_key(fresh_db):
    from integrations.social.e2e_key_service import (
        E2EKeyService, E2EKeyError)
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)
    with pytest.raises(E2EKeyError):
        E2EKeyService.publish_identity_key(
            db, cid, a.id, identity_key_b64='')


# ── rotate_identity_key ────────────────────────────────────────────

def test_rotate_marks_active_row_rotated(fresh_db):
    from sqlalchemy import text
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)
    E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IK==')
    assert E2EKeyService.rotate_identity_key(db, cid, a.id) is True
    # Second rotate is a no-op (no active row left)
    assert E2EKeyService.rotate_identity_key(db, cid, a.id) is False


def test_rotate_no_active_returns_false(fresh_db):
    """rotate without a prior publish is a benign no-op."""
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)
    assert E2EKeyService.rotate_identity_key(db, cid, a.id) is False


# ── get_active_keys ────────────────────────────────────────────────

def test_get_active_keys_skips_rotated(fresh_db):
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    cid = _seed_conversation(db, a.id)
    E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IKa==')
    E2EKeyService.publish_identity_key(
        db, cid, b.id, identity_key_b64='IKb==')
    # b rotates; only a's key remains active
    E2EKeyService.rotate_identity_key(db, cid, b.id)
    active = E2EKeyService.get_active_keys(db, cid)
    assert len(active) == 1
    assert active[0]['user_id'] == a.id
    assert active[0]['identity_key_b64'] == 'IKa=='


def test_get_active_keys_empty_for_unknown_conversation(fresh_db):
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    assert E2EKeyService.get_active_keys(db, 'no-such-conv') == []


# ── record_envelope + fetch_envelope ──────────────────────────────

def test_record_then_fetch_round_trips(fresh_db):
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    cid = _seed_conversation(db, a.id)
    # Need a real message row to FK against
    from sqlalchemy import text
    mid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO messages "
        "(id, parent_kind, parent_id, author_id, content) "
        "VALUES (:id, 'conversation', :cid, :aid, '<encrypted>')"),
        {'id': mid, 'cid': cid, 'aid': a.id})
    db.commit()

    env = E2EKeyService.record_envelope(
        db, message_id=mid, recipient_id=b.id,
        ciphertext_b64='ENCb==', ratchet_header_b64='HDRb==')
    assert env['ciphertext_b64'] == 'ENCb=='
    fetched = E2EKeyService.fetch_envelope(db, mid, b.id)
    assert fetched is not None
    assert fetched['id'] == env['id']
    assert fetched['ratchet_header_b64'] == 'HDRb=='


def test_record_envelope_dup_pair_raises_clean_error(fresh_db):
    """Pass-5 F7: UNIQUE(message_id, recipient_id) violation now
    raises E2EKeyError (clean service-layer error), not bare
    SQLAlchemy IntegrityError."""
    from integrations.social.e2e_key_service import (
        E2EKeyService, E2EKeyError)
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    cid = _seed_conversation(db, a.id)
    from sqlalchemy import text
    mid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO messages "
        "(id, parent_kind, parent_id, author_id, content) "
        "VALUES (:id, 'conversation', :cid, :aid, '<encrypted>')"),
        {'id': mid, 'cid': cid, 'aid': a.id})
    db.commit()

    E2EKeyService.record_envelope(
        db, mid, b.id, ciphertext_b64='C1==')
    with pytest.raises(E2EKeyError) as exc:
        E2EKeyService.record_envelope(
            db, mid, b.id, ciphertext_b64='C2==')
    assert 'already' in str(exc.value).lower()


def test_fetch_envelope_missing_returns_none(fresh_db):
    """A recipient with no envelope (e.g. joined after the message
    was sent) gets None — not an error.  Schema-level invariant."""
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    assert E2EKeyService.fetch_envelope(db, 'no-msg', 'no-user') is None


# ── e2e_dms feature flag default OFF ─────────────────────────────

def test_e2e_dms_flag_default_off():
    """Phase 9 ships dark — the flag must default to False so
    existing DMs continue to use cleartext storage."""
    from integrations.social.feature_flags import _DEFAULTS
    assert _DEFAULTS.get('e2e_dms') is False


# ── Pass-5 F8: list_members_without_keys helper ──────────────────

def test_list_members_without_keys_finds_unpublished(fresh_db):
    """Pass-5 F8: a sender needs to know which conversation members
    haven't published a key yet so the UI can warn before sending.
    """
    from sqlalchemy import text
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, b, c = _seed_users(db, 3)
    cid = _seed_conversation(db, a.id)
    # Add all three as conversation members (raw SQL since member rows
    # aren't otherwise needed in this test file).
    for uid in (a.id, b.id, c.id):
        db.execute(text(
            "INSERT INTO memberships "
            "(id, parent_kind, parent_id, member_id, agent_kind, role) "
            "VALUES (:id, 'conversation', :pid, :mid, 'human', 'member')"),
            {'id': str(uuid.uuid4()), 'pid': cid, 'mid': uid})
    db.commit()
    # Only a publishes
    E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IK==')
    missing = E2EKeyService.list_members_without_keys(db, cid)
    assert set(missing) == {b.id, c.id}


def test_list_members_without_keys_empty_when_all_published(fresh_db):
    from sqlalchemy import text
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    cid = _seed_conversation(db, a.id)
    for uid in (a.id, b.id):
        db.execute(text(
            "INSERT INTO memberships "
            "(id, parent_kind, parent_id, member_id, agent_kind, role) "
            "VALUES (:id, 'conversation', :pid, :mid, 'human', 'member')"),
            {'id': str(uuid.uuid4()), 'pid': cid, 'mid': uid})
        E2EKeyService.publish_identity_key(
            db, cid, uid, identity_key_b64=f'IK_{uid[:4]}==')
    db.commit()
    assert E2EKeyService.list_members_without_keys(db, cid) == []


# ── Pass-5 F1: tenant scoping closes cross-tenant snoop ───────────

def test_fetch_envelope_cross_tenant_returns_none(fresh_db):
    """Pass-5 F1: a tenant-A user querying for a tenant-B envelope
    by guessed (message_id, recipient_id) gets None — no leak.
    Identical shape to "envelope doesn't exist" so existence isn't
    enumerable across tenants."""
    from sqlalchemy import text
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    cid = _seed_conversation(db, a.id)
    mid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO messages "
        "(id, parent_kind, parent_id, author_id, content) "
        "VALUES (:id, 'conversation', :cid, :aid, '<encrypted>')"),
        {'id': mid, 'cid': cid, 'aid': a.id})
    db.commit()

    # Record envelope as tenant-A
    E2EKeyService.record_envelope(
        db, mid, b.id, ciphertext_b64='ENC==', tenant_id='tenant-A')
    # Tenant-B query for the same envelope returns None
    fetched_b = E2EKeyService.fetch_envelope(
        db, mid, b.id, tenant_id='tenant-B')
    assert fetched_b is None
    # Tenant-A query (matching) returns the envelope
    fetched_a = E2EKeyService.fetch_envelope(
        db, mid, b.id, tenant_id='tenant-A')
    assert fetched_a is not None


def test_get_active_keys_cross_tenant_filtered(fresh_db):
    """Pass-5 F1: get_active_keys filtered by tenant_id."""
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)
    E2EKeyService.publish_identity_key(
        db, cid, a.id, identity_key_b64='IK==', tenant_id='tenant-A')
    # tenant-B caller sees no keys
    assert E2EKeyService.get_active_keys(
        db, cid, tenant_id='tenant-B') == []
    # tenant-A caller sees the key
    keys_a = E2EKeyService.get_active_keys(db, cid, tenant_id='tenant-A')
    assert len(keys_a) == 1


# ── Pass-5 F9: tenant_id falls back to g.tenant_id ────────────────

def test_publish_resolves_tenant_id_from_flask_g(fresh_db):
    """Pass-5 F9: caller can omit tenant_id; service pulls it from
    g.tenant_id when in a Flask request.  Closes the foot-gun where
    forgetting tenant_id silently inserts an untenanted (NULL) row."""
    from sqlalchemy import text
    from flask import Flask, g
    from integrations.social.e2e_key_service import E2EKeyService
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    cid = _seed_conversation(db, a.id)

    app = Flask(__name__)
    with app.test_request_context():
        g.tenant_id = 'tenant-A'
        # Note: NO tenant_id arg
        result = E2EKeyService.publish_identity_key(
            db, cid, a.id, identity_key_b64='IK==')

    row = db.execute(text(
        "SELECT tenant_id FROM conversation_keys WHERE id = :id"),
        {'id': result['id']}).fetchone()
    assert row[0] == 'tenant-A'
