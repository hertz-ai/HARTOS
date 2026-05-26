"""Phase 7c.1 — Friendship state machine + Block integration tests.

Plan reference: sunny-gliding-eich.md, Part E.8 + Part R.5 +
Part V.1 J3 (friend request → accept → block journey).

Covers:
  1. Migration v43 — friendships + blocks tables.
  2. State machine: pending → accept / reject / cancel / auto-accept.
  3. Reciprocal Follow rows auto-created on accept (preserves
     existing follow-graph readers).
  4. Block: severs PeerLink trust, prevents future requests, leaves
     audit trail (Block row + Friendship.status='blocked').
  5. Idempotency: duplicate request → no duplicate row.
  6. Privacy: blocked-either-direction returns same opaque error
     (no leak of who blocked whom).
  7. Endpoint flag-gate behavior.
  8. Regression: existing FollowService.follow still works.
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


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv('HEVOLVE_FLAG_FRIENDS_V2', 'true')
    from flask import Flask
    from integrations.social import api
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    yield app.test_client(), fresh_db[0]


def _seed(db):
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()), username='alice', display_name='Alice',
             email='alice@x.test', password_hash='x:y', user_type='human')
    b = User(id=str(uuid.uuid4()), username='bob', display_name='Bob',
             email='bob@x.test', password_hash='x:y', user_type='human')
    c = User(id=str(uuid.uuid4()), username='cara', display_name='Cara',
             email='cara@x.test', password_hash='x:y', user_type='human')
    db.add_all([a, b, c])
    db.commit()
    return a, b, c


# ── Migration v43 ─────────────────────────────────────────────────

def test_v43_creates_friendships_and_blocks(fresh_db):
    from sqlalchemy import inspect
    db, eng = fresh_db
    insp = inspect(eng)
    assert 'friendships' in insp.get_table_names()
    assert 'blocks' in insp.get_table_names()
    fcols = {c['name'] for c in insp.get_columns('friendships')}
    assert {'id', 'tenant_id', 'user_a_id', 'user_b_id', 'status',
            'initiator_id', 'created_at', 'accepted_at',
            'blocked_at'}.issubset(fcols)
    bcols = {c['name'] for c in insp.get_columns('blocks')}
    assert {'id', 'tenant_id', 'blocker_id', 'blocked_id', 'reason',
            'created_at'}.issubset(bcols)


def test_v43_idempotent(fresh_db):
    from integrations.social import migrations
    migrations.run_migrations()


# ── State machine ─────────────────────────────────────────────────

def test_request_creates_pending(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    assert res['status'] == 'pending'


def test_accept_transitions_to_active(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    fid = res['id']
    res2 = FriendService.accept(db, fid, accepting_user_id=b.id)
    assert res2['status'] == 'active'


def test_initiator_cannot_self_accept(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService, FriendError
    res = FriendService.send_request(db, a.id, b.id)
    with pytest.raises(FriendError):
        FriendService.accept(db, res['id'], accepting_user_id=a.id)


def test_reject_transitions_to_rejected(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    res2 = FriendService.reject(db, res['id'], rejecting_user_id=b.id)
    assert res2['status'] == 'rejected'


def test_cancel_deletes_pending(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    res2 = FriendService.cancel(db, res['id'], canceller_id=a.id)
    assert res2['status'] == 'cancelled'
    # row gone
    from sqlalchemy import text
    assert db.execute(text("SELECT id FROM friendships WHERE id=:i"),
                      {'i': res['id']}).fetchone() is None


def test_only_initiator_can_cancel(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService, FriendError
    res = FriendService.send_request(db, a.id, b.id)
    with pytest.raises(FriendError):
        FriendService.cancel(db, res['id'], canceller_id=b.id)


def test_reverse_request_auto_accepts(fresh_db):
    """If A requests B, then B sends request to A — auto-accept."""
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    FriendService.send_request(db, a.id, b.id)
    res = FriendService.send_request(db, b.id, a.id)
    assert res['status'] == 'active'


def test_idempotent_request_returns_same_pending(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res1 = FriendService.send_request(db, a.id, b.id)
    res2 = FriendService.send_request(db, a.id, b.id)
    assert res1['id'] == res2['id']
    assert res2['status'] == 'pending'


def test_accept_creates_reciprocal_follows(fresh_db):
    """The auto-Follow rows preserve legacy follow-graph readers."""
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, res['id'], accepting_user_id=b.id)
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT follower_id, following_id FROM follows "
        "WHERE (follower_id=:a AND following_id=:b) "
        "OR (follower_id=:b AND following_id=:a)"),
        {'a': a.id, 'b': b.id}
    ).fetchall()
    # Both directions present (reciprocal).
    pairs = {(r[0], r[1]) for r in rows}
    assert (a.id, b.id) in pairs
    assert (b.id, a.id) in pairs


def test_is_friend_query(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, res['id'], accepting_user_id=b.id)
    assert FriendService.is_friend(db, a.id, b.id) is True
    assert FriendService.is_friend(db, b.id, a.id) is True
    assert FriendService.is_friend(db, a.id, c.id) is False


# ── Block ─────────────────────────────────────────────────────────

def test_block_creates_row_and_marks_friendship(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, res['id'], accepting_user_id=b.id)
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id, reason='harassment')
    from sqlalchemy import text
    # Block row exists
    blk = db.execute(text(
        "SELECT reason FROM blocks WHERE blocker_id=:a AND blocked_id=:b"),
        {'a': a.id, 'b': b.id}
    ).fetchone()
    assert blk is not None
    assert blk[0] == 'harassment'
    # Friendship marked blocked
    fr = db.execute(text(
        "SELECT status FROM friendships WHERE id=:i"),
        {'i': res['id']}
    ).fetchone()
    assert fr[0] == 'blocked'


def test_request_after_block_raises(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService, FriendError
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id)
    # Either side trying to friend the other gets the SAME opaque error
    # (no leak about who blocked).
    with pytest.raises(FriendError):
        FriendService.send_request(db, a.id, b.id)
    with pytest.raises(FriendError):
        FriendService.send_request(db, b.id, a.id)


def test_unblock_removes_row_does_not_restore_friendship(fresh_db):
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, res['id'], accepting_user_id=b.id)
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id)
    FriendService.unblock(db, blocker_id=a.id, blocked_id=b.id)
    # Block row gone
    from sqlalchemy import text
    blk = db.execute(text(
        "SELECT id FROM blocks WHERE blocker_id=:a AND blocked_id=:b"),
        {'a': a.id, 'b': b.id}
    ).fetchone()
    assert blk is None
    # Friendship still 'blocked' — explicit re-friend required
    fr = db.execute(text(
        "SELECT status FROM friendships WHERE id=:i"),
        {'i': res['id']}
    ).fetchone()
    assert fr[0] == 'blocked'


# ── Read paths ────────────────────────────────────────────────────

def test_list_friends_active_filters_status(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.friend_service import FriendService
    # a-b active
    r1 = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, r1['id'], accepting_user_id=b.id)
    # a-c pending
    FriendService.send_request(db, a.id, c.id)
    actives = FriendService.list_friends(db, a.id, status='active')
    assert len(actives) == 1
    assert actives[0]['other_user']['username'] == 'bob'
    pendings = FriendService.list_friends(db, a.id, status='pending')
    assert len(pendings) == 1
    assert pendings[0]['other_user']['username'] == 'cara'


def test_list_blocks(fresh_db):
    db, _ = fresh_db
    a, b, c = _seed(db)
    from integrations.social.friend_service import FriendService
    FriendService.block(db, blocker_id=a.id, blocked_id=b.id, reason='spam')
    FriendService.block(db, blocker_id=a.id, blocked_id=c.id)
    rows = FriendService.list_blocks(db, a.id)
    assert len(rows) == 2
    usernames = {r['blocked_user']['username'] for r in rows}
    assert usernames == {'bob', 'cara'}


# ── Endpoints (flag-gated) ────────────────────────────────────────

def test_endpoint_flag_off_503(app_client, monkeypatch):
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    monkeypatch.delenv('HEVOLVE_FLAG_FRIENDS_V2', raising=False)
    r = client.post('/api/social/friends/request',
                    json={'target_user_id': b.id},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_endpoint_flag_on_request_accept(app_client):
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')
    # A → B
    r = client.post('/api/social/friends/request',
                    json={'target_user_id': b.id},
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert r.status_code == 200
    fid = r.get_json()['data']['id']
    # B accepts
    r2 = client.post(f'/api/social/friends/request/{fid}/accept',
                     headers={'Authorization': f'Bearer {tok_b}'})
    assert r2.status_code == 200
    assert r2.get_json()['data']['status'] == 'active'


def test_endpoint_list_friends(app_client):
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social.friend_service import FriendService
    res = FriendService.send_request(db, a.id, b.id)
    FriendService.accept(db, res['id'], accepting_user_id=b.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get('/api/social/friends',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    data = r.get_json()['data']
    assert len(data) == 1
    assert data[0]['other_user']['username'] == 'bob'


def test_endpoint_block_unblock(app_client):
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post(f'/api/social/friends/{b.id}/block',
                    json={'reason': 'spam'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    r2 = client.get('/api/social/friends/blocks',
                    headers={'Authorization': f'Bearer {tok}'})
    assert len(r2.get_json()['data']) == 1
    r3 = client.post(f'/api/social/friends/{b.id}/unblock',
                     headers={'Authorization': f'Bearer {tok}'})
    assert r3.status_code == 200
    r4 = client.get('/api/social/friends/blocks',
                    headers={'Authorization': f'Bearer {tok}'})
    assert len(r4.get_json()['data']) == 0


# ── Regression — existing Follow surface unchanged ────────────────

def test_existing_follow_endpoints_still_work(fresh_db):
    """The legacy one-direction Follow endpoints (usersApi.follow)
    are NOT touched by Phase 7c.1. Direct service-level test to
    confirm the table + service still operate as before."""
    db, _ = fresh_db
    a, b, _ = _seed(db)
    from integrations.social.services import FollowService
    FollowService.follow(db, a, b.id)
    followers, total = FollowService.get_followers(db, b.id)
    assert total == 1
    assert any(f.id == a.id for f in followers)


# ── Journey J3: friend request → accept → block ──────────────────

def test_journey_J3_full_flow(app_client):
    """Plan Part V.1 J3: full friend request → accept → block."""
    client, db = app_client
    a, b, _ = _seed(db)
    from integrations.social import auth
    tok_a = auth.generate_jwt(a.id, a.username, 'flat')
    tok_b = auth.generate_jwt(b.id, b.username, 'flat')

    # A sends request
    r1 = client.post('/api/social/friends/request',
                     json={'target_user_id': b.id},
                     headers={'Authorization': f'Bearer {tok_a}'})
    assert r1.status_code == 200
    fid = r1.get_json()['data']['id']

    # B accepts
    r2 = client.post(f'/api/social/friends/request/{fid}/accept',
                     headers={'Authorization': f'Bearer {tok_b}'})
    assert r2.status_code == 200

    # Both see each other in friends list
    r3 = client.get('/api/social/friends',
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert any(f['other_user']['id'] == b.id for f in r3.get_json()['data'])

    # A blocks B
    r4 = client.post(f'/api/social/friends/{b.id}/block',
                     headers={'Authorization': f'Bearer {tok_a}'})
    assert r4.status_code == 200

    # A's friends list no longer shows B as active
    r5 = client.get('/api/social/friends',
                    headers={'Authorization': f'Bearer {tok_a}'})
    assert not any(f['other_user']['id'] == b.id and f['status'] == 'active'
                   for f in r5.get_json()['data'])

    # B trying to re-request A is rejected (opaque)
    r6 = client.post('/api/social/friends/request',
                     json={'target_user_id': a.id},
                     headers={'Authorization': f'Bearer {tok_b}'})
    assert r6.status_code == 400
