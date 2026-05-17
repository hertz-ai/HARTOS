"""Phase 7d — calls REST + service backend.

Plan reference: sunny-gliding-eich.md, Part E.4 + E.7 + E.12.

Coverage:
  - Migration v49 creates the three tables (call_sessions,
    call_participants, agent_join_grants) with the expected indexes.
  - CallService.create idempotent on (parent, active call): two
    starters get the same call.
  - Membership gate: non-member start / join / token requests 404.
  - Participant lifecycle: join → leave → re-join is clean.
  - Single-active invariant: UNIQUE-where-left_at-IS-NULL prevents
    a user from holding two active rows in the same call.
  - End: only starter or parent admin can end; idempotent.
  - AgentJoinGrant: owner-only grants, scope enforced on attach.
  - LiveKitService.issue_token defaults to p2p_mesh when LIVEKIT_URL
    unset (flat / regional / Nunba bundled deploys).
  - REST surface 503s when calls_v1 flag is off.

Style mirrors test_phase7c5_post_privacy.py.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Fixtures ───────────────────────────────────────────────────────

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
    monkeypatch.setenv('HEVOLVE_FLAG_CALLS_V1', 'true')
    from flask import Flask
    from integrations.social import api
    from integrations.social.api_conversations import conversations_bp
    from integrations.social.api_calls import calls_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(conversations_bp)
    app.register_blueprint(calls_bp)
    yield app.test_client(), fresh_db[0]


def _seed_users(db, n=3):
    from integrations.social.models import User
    users = []
    for i in range(n):
        u = User(id=str(uuid.uuid4()),
                 username=f'u{i}_{uuid.uuid4().hex[:6]}',
                 display_name=f'U{i}',
                 email=f'u{i}_{uuid.uuid4().hex[:6]}@x.test',
                 password_hash='x:y',
                 user_type='human')
        users.append(u)
    db.add_all(users)
    db.commit()
    return users


def _seed_agent(db, owner_id):
    """Create an agent user with the canonical SocialUser.owner_id
    set.  (Plan C.3 calls this `agent_owner_id`; the live schema is
    `owner_id` — same semantic.)"""
    from integrations.social.models import User
    a = User(id=str(uuid.uuid4()),
             username=f'agent_{uuid.uuid4().hex[:6]}',
             display_name='Agent',
             email=f'agent_{uuid.uuid4().hex[:6]}@x.test',
             password_hash='x:y',
             user_type='agent')
    a.owner_id = owner_id
    db.add(a)
    db.commit()
    return a


def _seed_community(db, owner_id, members=None):
    from sqlalchemy import text
    from integrations.social.models import Community
    cid = str(uuid.uuid4())
    com = Community(id=cid, name=f'c_{uuid.uuid4().hex[:6]}',
                    display_name='C', description='',
                    creator_id=owner_id, is_private=False)
    db.add(com)
    db.commit()
    db.execute(text(
        "INSERT INTO memberships "
        "(id, parent_kind, parent_id, member_id, agent_kind, role) "
        "VALUES (:id, 'community', :pid, :mid, 'human', 'admin')"),
        {'id': str(uuid.uuid4()), 'pid': cid, 'mid': owner_id})
    for m in (members or []):
        db.execute(text(
            "INSERT INTO memberships "
            "(id, parent_kind, parent_id, member_id, agent_kind, role) "
            "VALUES (:id, 'community', :pid, :mid, 'human', 'member')"),
            {'id': str(uuid.uuid4()), 'pid': cid, 'mid': m})
    db.commit()
    return com


# ── Migration ───────────────────────────────────────────────────────

def test_v49_creates_three_tables(fresh_db):
    """Migration v49 creates call_sessions + call_participants +
    agent_join_grants.  All present after run_migrations."""
    from sqlalchemy import text
    db, _ = fresh_db
    for tbl in ('call_sessions', 'call_participants', 'agent_join_grants'):
        rows = db.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name = :n"),
            {'n': tbl}).fetchall()
        assert rows, f"v49 did not create table {tbl}"


# ── CallService.create ──────────────────────────────────────────────

def test_create_call_idempotent_on_active_call(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService
    s1 = CallService.create(db, 'community', com.id, a.id, kind='voice')
    s2 = CallService.create(db, 'community', com.id, b.id, kind='voice')
    assert s1['id'] == s2['id'], (
        "two starters on the same parent must converge on the existing "
        "active call instead of creating a duplicate")


def test_create_call_refuses_non_member(fresh_db):
    db, _ = fresh_db
    a, c = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id)  # c is NOT a member
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.create(db, 'community', com.id, c.id, kind='voice')


def test_create_call_starter_auto_joined(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    parts = CallService.list_participants(db, sess['id'])
    assert len(parts) == 1
    assert parts[0]['user_id'] == a.id


def test_create_unsupported_kind_raises(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.create(db, 'community', com.id, a.id, kind='telegraph')


# ── Participant lifecycle ──────────────────────────────────────────

def test_join_idempotent(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    p1 = CallService.join(db, sess['id'], b.id)
    p2 = CallService.join(db, sess['id'], b.id)
    assert p1['id'] == p2['id'], (
        "re-joining must return the existing active row, not duplicate")


def test_join_then_leave_then_rejoin(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    p1 = CallService.join(db, sess['id'], b.id)
    assert CallService.leave(db, sess['id'], b.id) is True
    p2 = CallService.join(db, sess['id'], b.id)
    # New row after leave — different id, both present in include_left
    assert p1['id'] != p2['id']
    all_parts = CallService.list_participants(
        db, sess['id'], include_left=True)
    by_user = [p for p in all_parts if p['user_id'] == b.id]
    assert len(by_user) == 2


def test_join_non_member_refused(fresh_db):
    db, _ = fresh_db
    a, c = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id)  # c not a member
    from integrations.social.call_service import CallService, CallError
    sess = CallService.create(db, 'community', com.id, a.id)
    with pytest.raises(CallError):
        CallService.join(db, sess['id'], c.id)


# ── End ─────────────────────────────────────────────────────────────

def test_end_call_by_starter(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    ended = CallService.end(db, sess['id'], a.id)
    assert ended['ended_at'] is not None
    # Active participant rows are flipped to left_at
    parts = CallService.list_participants(db, sess['id'])
    assert parts == []  # only active rows


def test_end_call_idempotent(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    e1 = CallService.end(db, sess['id'], a.id)
    e2 = CallService.end(db, sess['id'], a.id)
    assert e1['ended_at'] == e2['ended_at']


def test_end_call_refuses_non_starter_non_admin(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService, CallError
    sess = CallService.create(db, 'community', com.id, a.id)
    with pytest.raises(CallError):
        CallService.end(db, sess['id'], b.id)


# ── Agent join grants ─────────────────────────────────────────────

def test_grant_agent_idempotent_updates_scope(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    g1 = CallService.grant_agent(
        db, agent.id, a.id, 'community', com.id,
        scope={'can_voice': True, 'can_screen': False})
    g2 = CallService.grant_agent(
        db, agent.id, a.id, 'community', com.id,
        scope={'can_voice': True, 'can_screen': True})
    assert g1['id'] == g2['id'], (
        "re-granting must update scope on the existing row, not insert dup")
    assert g2['scope']['can_screen'] is True


def test_grant_agent_only_owner_can_grant(fresh_db):
    db, _ = fresh_db
    a, b = _seed_users(db, 2)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        # b is not the owner of `agent`
        CallService.grant_agent(
            db, agent.id, b.id, 'community', com.id,
            scope={'can_voice': True})


def test_grant_system_agent_refused_for_non_admin(fresh_db):
    """Pass-4 P4-3 fix: ownerless (system) agents must NOT be
    grantable by arbitrary authenticated users.  Previously the
    ownership check short-circuited when owner_id IS NULL, allowing
    any user to grant can_voice / can_screen on a system agent.
    """
    from sqlalchemy import text
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    # Create a system agent (owner_id IS NULL)
    from integrations.social.models import User
    sa = User(id=str(uuid.uuid4()), username=f'sys_{uuid.uuid4().hex[:6]}',
              display_name='SystemAgent',
              email=f'sa_{uuid.uuid4().hex[:6]}@x.test',
              password_hash='x:y', user_type='agent')
    sa.owner_id = None
    db.add(sa)
    db.commit()
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError) as exc:
        CallService.grant_agent(
            db, sa.id, a.id, 'community', com.id,
            scope={'can_voice': True})
    assert 'admin' in str(exc.value).lower()


def test_grant_system_agent_allowed_for_platform_admin(fresh_db):
    """Mirror to the previous test: a platform admin CAN grant a
    system agent.  Locks the explicit policy."""
    from sqlalchemy import text
    db, _ = fresh_db
    admin, = _seed_users(db, 1)
    # Promote to platform admin
    db.execute(text("UPDATE users SET is_admin = 1 WHERE id = :id"),
               {'id': admin.id})
    db.commit()
    from integrations.social.models import User
    sa = User(id=str(uuid.uuid4()), username=f'sys_{uuid.uuid4().hex[:6]}',
              display_name='SystemAgent',
              email=f'sa_{uuid.uuid4().hex[:6]}@x.test',
              password_hash='x:y', user_type='agent')
    sa.owner_id = None
    db.add(sa)
    db.commit()
    com = _seed_community(db, owner_id=admin.id)
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(
        db, sa.id, admin.id, 'community', com.id,
        scope={'can_voice': True})
    assert grant['agent_id'] == sa.id
    assert grant['scope']['can_voice'] is True


def test_revoke_grant_idempotent(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    g = CallService.grant_agent(
        db, agent.id, a.id, 'community', com.id,
        scope={'can_voice': True})
    assert CallService.revoke_agent(db, g['id'], a.id) is True
    # Second revoke is a no-op
    assert CallService.revoke_agent(db, g['id'], a.id) is False


def test_attach_agent_requires_can_voice(fresh_db):
    """attach_agent on a 'voice' call needs scope.can_voice=True."""
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService, CallError
    sess = CallService.create(db, 'community', com.id, a.id, kind='voice')
    # Grant without can_voice
    CallService.grant_agent(
        db, agent.id, a.id, 'community', com.id,
        scope={'can_voice': False})
    with pytest.raises(CallError):
        CallService.attach_agent(db, sess['id'], agent.id)


def test_attach_agent_with_grant_succeeds(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    from sqlalchemy import text
    # Add agent as a member of the community so the membership
    # gate inside join() passes.
    db.execute(text(
        "INSERT INTO memberships "
        "(id, parent_kind, parent_id, member_id, agent_kind, role) "
        "VALUES (:id, 'community', :pid, :mid, 'agent', 'member')"),
        {'id': str(uuid.uuid4()), 'pid': com.id, 'mid': agent.id})
    db.commit()
    sess = CallService.create(db, 'community', com.id, a.id, kind='voice')
    CallService.grant_agent(
        db, agent.id, a.id, 'community', com.id,
        scope={'can_voice': True})
    p = CallService.attach_agent(db, sess['id'], agent.id)
    assert p['agent_kind'] == 'agent'
    assert p['device_kind'] == 'agent_bridge'
    # Phase 7d.B — bridge worker should have spun up alongside.
    bridges = AgentVoiceBridge.list_active(call_id=sess['id'])
    assert any(b['agent_id'] == agent.id for b in bridges)
    AgentVoiceBridge.shutdown_all()


def test_attach_agent_without_grant_refused(fresh_db):
    db, _ = fresh_db
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService, CallError
    sess = CallService.create(db, 'community', com.id, a.id, kind='voice')
    with pytest.raises(CallError):
        CallService.attach_agent(db, sess['id'], agent.id)


# ── LiveKit token issuance ─────────────────────────────────────────

def test_token_falls_back_to_p2p_when_no_livekit_config(monkeypatch):
    """Flat / regional / Nunba bundled have LIVEKIT_URL unset.
    issue_token must return mode='p2p_mesh' so clients run a WebRTC
    P2P mesh signaled over PeerLink instead of trying to connect to
    a non-existent LiveKit instance."""
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    monkeypatch.delenv('LIVEKIT_API_KEY', raising=False)
    monkeypatch.delenv('LIVEKIT_API_SECRET', raising=False)
    from integrations.social.livekit_service import LiveKitService
    r = LiveKitService.issue_token('call-1', 'user-1')
    assert r['mode'] == 'p2p_mesh'
    assert r['call_id'] == 'call-1'
    assert 'reason' in r


def test_token_uses_livekit_when_configured(monkeypatch):
    """Central deploy has LIVEKIT_URL set.  Token result includes
    mode='livekit' (signed JWT) when livekit-api SDK is installed,
    'livekit_pending' otherwise.  Either way the URL + metadata
    flow through and the client knows whether infra is ready."""
    monkeypatch.setenv('LIVEKIT_URL', 'wss://livekit.example')
    monkeypatch.setenv('LIVEKIT_API_KEY', 'k')
    monkeypatch.setenv('LIVEKIT_API_SECRET', 's')
    from integrations.social.livekit_service import LiveKitService
    r = LiveKitService.issue_token('call-1', 'user-1', is_agent=True)
    assert r['mode'] in ('livekit', 'livekit_pending')
    assert r['url'] == 'wss://livekit.example'
    assert r['metadata']['agent_kind'] == 'agent'


# ── REST surface ──────────────────────────────────────────────────

def test_start_call_endpoint_creates_session(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/calls',
                    json={'parent_kind': 'community', 'parent_id': com.id,
                          'kind': 'voice'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['data']['kind'] == 'voice'
    assert body['data']['started_by'] == a.id


def test_start_call_404_for_non_member(app_client):
    """non-member starts → 404 (not 403) so the parent's existence is
    not leaked.  Same shape the rest of the API uses for tenant
    isolation + privacy gates."""
    client, db = app_client
    a, c = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id)  # c not a member
    from integrations.social import auth
    tok = auth.generate_jwt(c.id, c.username, 'flat')
    r = client.post('/api/social/calls',
                    json={'parent_kind': 'community', 'parent_id': com.id,
                          'kind': 'voice'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 404


def test_calls_endpoints_503_when_flag_off(fresh_db, monkeypatch):
    """Flag-off → every /api/social/calls endpoint returns 503."""
    monkeypatch.delenv('HEVOLVE_FLAG_CALLS_V1', raising=False)
    from flask import Flask
    from integrations.social import api, auth
    from integrations.social.api_calls import calls_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(calls_bp)
    client = app.test_client()
    db = fresh_db[0]
    a, = _seed_users(db, 1)
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/calls',
                    json={'parent_kind': 'community', 'parent_id': 'cid',
                          'kind': 'voice'},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 503


def test_get_call_includes_participants(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.get(f'/api/social/calls/{sess["id"]}',
                   headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    body = r.get_json()
    assert 'participants' in body['data']
    assert len(body['data']['participants']) == 1


def test_token_endpoint_returns_p2p_when_no_livekit(app_client, monkeypatch):
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    client, db = app_client
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post(f'/api/social/calls/{sess["id"]}/token',
                    json={},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200
    assert r.get_json()['data']['mode'] == 'p2p_mesh'


def test_token_410_when_call_ended(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    CallService.end(db, sess['id'], a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post(f'/api/social/calls/{sess["id"]}/token',
                    json={},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 410


def test_end_call_403_for_non_starter_non_admin(app_client):
    client, db = app_client
    a, b = _seed_users(db, 2)
    com = _seed_community(db, owner_id=a.id, members=[b.id])
    from integrations.social.call_service import CallService
    sess = CallService.create(db, 'community', com.id, a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(b.id, b.username, 'flat')
    r = client.post(f'/api/social/calls/{sess["id"]}/end',
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 403


def test_create_agent_grant_endpoint(app_client):
    client, db = app_client
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=a.id)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social import auth
    tok = auth.generate_jwt(a.id, a.username, 'flat')
    r = client.post('/api/social/agent-grants',
                    json={'agent_id': agent.id,
                          'parent_kind': 'community',
                          'parent_id': com.id,
                          'scope': {'can_voice': True}},
                    headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['data']['agent_id'] == agent.id
    assert body['data']['scope']['can_voice'] is True
