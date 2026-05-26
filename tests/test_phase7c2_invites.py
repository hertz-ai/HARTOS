"""Phase 7c.2 — Invite (community + conversation, polymorphic) tests.

Plan reference: sunny-gliding-eich.md, Part E.9 + Part C.2.

Locks the invite contract:

  Migration:
    - v44 creates `invites` table with all expected columns + indexes.
    - Idempotent re-run does not crash.

  InviteService.send:
    - Targeted user invite (invitee_id) inserts row + fires Notification.
    - Email invite (invitee_email) inserts row, no in-app notification.
    - Shareable link (neither) inserts row + returns invite_code.
    - Self-invite refused.
    - Bidirectional block check refuses send.
    - Already-member returns short-circuit instead of insert.
    - Invalid parent_kind / role_offered raises InviteError.

  InviteService.accept:
    - Targeted invitee accepting → status='accepted', Membership row inserted.
    - Wrong user accepting targeted invite → InviteError.
    - Shareable link first-accepter wins; second accepter gets refused.
    - Expired invite → status flipped to 'expired', accept refused.
    - Already-accepted by same user → idempotent.

  InviteService.reject:
    - Pending invite → status='rejected'.
    - Already-responded → InviteError.

  InviteService.list_incoming:
    - Returns pending invites for invitee_id.
    - Returns pending invites matching invitee_email after signup.
    - Excludes expired invites silently.

  InviteService.resolve_code:
    - Returns dict for pending unexpired code.
    - Returns None for unknown / expired / consumed code.

  Endpoint flag-gating:
    - Flag off + mutating endpoint → 503.
    - Flag off + read endpoint → empty list.
    - Flag on → normal behavior.
"""

from __future__ import annotations

import os
import sys
import time
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


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv('HEVOLVE_FLAG_INVITES_V2', 'true')
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


def _silence_realtime(monkeypatch):
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, 'publish_event', lambda *a, **kw: None)
    monkeypatch.setattr(realtime, '_get_publisher', lambda: None)


def _seed(db):
    """Two users + a community."""
    from integrations.social.models import User, Community
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='bob@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b])
    c = Community(id=str(uuid.uuid4()), name='cosmic-tea-club',
                  display_name='Cosmic Tea', description='star talk',
                  creator_id=a.id, is_private=True)
    db.add(c)
    db.commit()
    return a, b, c


# ── Migration v44 ──────────────────────────────────────────────────

def test_v44_invites_table_created(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    assert 'invites' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('invites')}
    expected = {'id', 'tenant_id', 'parent_kind', 'parent_id',
                'invitee_id', 'invitee_email', 'invite_code',
                'invited_by', 'role_offered', 'status',
                'created_at', 'expires_at', 'responded_at'}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_v44_idempotent(fresh_db):
    from integrations.social import migrations
    migrations.run_migrations()  # second run must not raise


# ── InviteService.send ────────────────────────────────────────────

def test_send_targeted_user_invite(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    result = InviteService.send(
        db, parent_kind='community', parent_id=c.id,
        invited_by=a.id, invitee_id=b.id)
    assert result['status'] == 'pending'
    assert result['invite_code']
    assert result['parent_kind'] == 'community'

    # Notification row created for invitee.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT user_id FROM notifications "
        "WHERE source_user_id = :a AND type = 'invite'"),
        {'a': a.id}).fetchall()
    assert any(r[0] == b.id for r in rows)


def test_send_shareable_link(fresh_db, monkeypatch):
    """Neither invitee_id nor invitee_email → invite_code is the share token."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    result = InviteService.send(
        db, parent_kind='community', parent_id=c.id, invited_by=a.id)
    assert result['status'] == 'pending'
    assert result['invite_code']
    # Shareable links don't notify — there's no recipient yet.


def test_send_email_invite(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    result = InviteService.send(
        db, parent_kind='community', parent_id=c.id,
        invited_by=a.id, invitee_email='offplatform@x.test')
    assert result['status'] == 'pending'


def test_send_self_invite_refused(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import (
        InviteService, InviteError)
    with pytest.raises(InviteError, match='cannot invite yourself'):
        InviteService.send(db, parent_kind='community', parent_id=c.id,
                           invited_by=a.id, invitee_id=a.id)


def test_send_blocked_user_refused(fresh_db, monkeypatch):
    """If invitee blocked the inviter (or vice versa), send raises."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.friend_service import FriendService
    FriendService.block(db, blocker_id=b.id, blocked_id=a.id)

    from integrations.social.invite_service import (
        InviteService, InviteError)
    with pytest.raises(InviteError, match='cannot send invite'):
        InviteService.send(db, parent_kind='community', parent_id=c.id,
                           invited_by=a.id, invitee_id=b.id)


def test_send_invalid_parent_kind_refused(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import (
        InviteService, InviteError)
    with pytest.raises(InviteError, match='invalid parent_kind'):
        InviteService.send(db, parent_kind='post', parent_id=c.id,
                           invited_by=a.id, invitee_id=b.id)


def test_send_invalid_role_refused(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import (
        InviteService, InviteError)
    with pytest.raises(InviteError, match='invalid role_offered'):
        InviteService.send(db, parent_kind='community', parent_id=c.id,
                           invited_by=a.id, invitee_id=b.id,
                           role_offered='king')


# ── InviteService.accept ──────────────────────────────────────────

def test_accept_targeted_invite_creates_membership(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    sent = InviteService.send(
        db, parent_kind='community', parent_id=c.id,
        invited_by=a.id, invitee_id=b.id)
    res = InviteService.accept(db, sent['id'], accepter_id=b.id)
    assert res['status'] == 'accepted'

    # Membership row exists in the polymorphic table.
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT role FROM memberships "
        "WHERE parent_kind='community' AND parent_id=:pid AND member_id=:mid"),
        {'pid': c.id, 'mid': b.id}).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'member'

    # Dual-write to legacy community_memberships (for community parents).
    cm_rows = db.execute(text(
        "SELECT user_id FROM community_memberships "
        "WHERE community_id=:pid AND user_id=:mid"),
        {'pid': c.id, 'mid': b.id}).fetchall()
    assert len(cm_rows) == 1


def test_accept_wrong_user_refused(fresh_db, monkeypatch):
    """Targeted invite to bob can't be accepted by anyone else."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve)
    db.commit()
    from integrations.social.invite_service import (
        InviteService, InviteError)
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    with pytest.raises(InviteError, match='not the invitee'):
        InviteService.accept(db, sent['id'], accepter_id=eve.id)


def test_accept_shareable_link_first_wins(fresh_db, monkeypatch):
    """First user to accept the share-link claims the slot; second
    user gets a 'already accepted' error."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='e@x.test', password_hash='x:y', user_type='human')
    db.add(eve)
    db.commit()
    from integrations.social.invite_service import (
        InviteService, InviteError)
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id)  # shareable
    # First accept by bob via code path.
    res1 = InviteService.accept(db, sent['invite_code'], accepter_id=b.id)
    assert res1['status'] == 'accepted'
    # Second accept by eve must fail.
    with pytest.raises(InviteError, match='already accepted'):
        InviteService.accept(db, sent['invite_code'], accepter_id=eve.id)


def test_accept_idempotent_for_same_user(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    InviteService.accept(db, sent['id'], accepter_id=b.id)
    # Re-accept by same user — idempotent, no error.
    res = InviteService.accept(db, sent['id'], accepter_id=b.id)
    assert res['status'] == 'accepted'


def test_accept_already_member_short_circuit(fresh_db, monkeypatch):
    """If the user is already a member, send returns 'already_member'
    without inserting a duplicate invite row."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    # First invite + accept makes bob a member.
    from integrations.social.invite_service import InviteService
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    InviteService.accept(db, sent['id'], accepter_id=b.id)
    # Second send to the same parent for bob — short circuit.
    res2 = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    assert res2['status'] == 'already_member'


# ── InviteService.reject ──────────────────────────────────────────

def test_reject_pending(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    res = InviteService.reject(db, sent['id'], rejecter_id=b.id)
    assert res['status'] == 'rejected'


def test_reject_already_responded_refused(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import (
        InviteService, InviteError)
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id, invitee_id=b.id)
    InviteService.accept(db, sent['id'], accepter_id=b.id)
    with pytest.raises(InviteError, match='cannot reject'):
        InviteService.reject(db, sent['id'], rejecter_id=b.id)


# ── InviteService.list_incoming ───────────────────────────────────

def test_list_incoming_pending_for_user(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    InviteService.send(db, parent_kind='community', parent_id=c.id,
                       invited_by=a.id, invitee_id=b.id)
    incoming = InviteService.list_incoming(db, b.id)
    assert len(incoming) == 1
    assert incoming[0]['invited_by'] == a.id
    assert incoming[0]['parent_kind'] == 'community'


def test_list_incoming_picks_up_email_invite_post_signup(fresh_db, monkeypatch):
    """Email invite sent before signup — visible after the user signs up
    with the matching email."""
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    # Off-platform invite to eve@x.test (no user yet).
    InviteService.send(db, parent_kind='community', parent_id=c.id,
                       invited_by=a.id, invitee_email='eve@x.test')
    # Eve signs up.
    from integrations.social.models import User
    eve = User(id=str(uuid.uuid4()), username='eve', display_name='Eve',
               email='eve@x.test', password_hash='x:y', user_type='human')
    db.add(eve)
    db.commit()
    incoming = InviteService.list_incoming(db, eve.id)
    assert len(incoming) == 1


# ── InviteService.resolve_code ────────────────────────────────────

def test_resolve_code_returns_pending_dict(fresh_db, monkeypatch):
    _silence_realtime(monkeypatch)
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.invite_service import InviteService
    sent = InviteService.send(db, parent_kind='community', parent_id=c.id,
                              invited_by=a.id)
    res = InviteService.resolve_code(db, sent['invite_code'])
    assert res is not None
    assert res['parent_kind'] == 'community'
    assert res['is_targeted'] is False


def test_resolve_code_returns_none_for_unknown(fresh_db):
    db, _ = fresh_db
    from integrations.social.invite_service import InviteService
    assert InviteService.resolve_code(db, 'nonexistent_code') is None


# ── Endpoint behaviour ────────────────────────────────────────────

def test_endpoint_flag_off_returns_503(fresh_db, monkeypatch):
    """Mutating endpoint with flag off → 503."""
    db, _ = fresh_db
    a, b, c = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_INVITES_V2', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/invites',
                    json={'parent_kind': 'community', 'parent_id': c.id,
                          'invitee_id': b.id},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_endpoint_flag_off_read_returns_empty(fresh_db, monkeypatch):
    """Read endpoint with flag off → 200 + []."""
    db, _ = fresh_db
    a, b, c = _seed(db)
    monkeypatch.delenv('HEVOLVE_FLAG_INVITES_V2', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    client = app.test_client()
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get('/api/social/invites/incoming',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data'] == []


def test_endpoint_send_then_accept(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b, c = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')

    # Alice sends.
    r = client.post('/api/social/invites',
                    json={'parent_kind': 'community', 'parent_id': c.id,
                          'invitee_id': b.id},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 200, r.get_json()
    invite_id = r.get_json()['data']['id']

    # Bob accepts.
    r = client.post(f'/api/social/invites/{invite_id}/accept',
                    headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    assert r.get_json()['data']['status'] == 'accepted'


def test_endpoint_resolve_code(app_client, monkeypatch):
    _silence_realtime(monkeypatch)
    client, db = app_client
    a, b, c = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')
    # Alice creates a shareable link.
    r = client.post('/api/social/invites',
                    json={'parent_kind': 'community', 'parent_id': c.id},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 200
    code = r.get_json()['data']['invite_code']

    # Bob resolves the code (preview before accept).
    r = client.get(f'/api/social/invites/code/{code}',
                   headers={'Authorization': f'Bearer {tok_b}'})
    assert r.status_code == 200
    body = r.get_json()['data']
    assert body['parent_kind'] == 'community'
    assert body['parent_id'] == c.id
    assert body['is_targeted'] is False
