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


def _machine_owner_and_stranger_agent(db, monkeypatch):
    """On a flat node -- a machine a person owns -- this machine's owner
    (HEVOLVE_OWNER_USER_ID, set by Nunba at boot) and an agent someone else
    wrote, in a community the owner is in."""
    me, author = _seed_users(db, 2)
    # The tier is read by security.key_delegation.get_node_tier; with no
    # master key in the environment it never reaches key material.
    monkeypatch.delenv('HEVOLVE_MASTER_PRIVATE_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', me.id)
    agent = _seed_agent(db, owner_id=author.id)
    com = _seed_community(db, owner_id=me.id)
    return me, author, agent, com


def test_on_a_regional_node_the_machine_owner_variable_grants_nothing(
        fresh_db, monkeypatch, tmp_path):
    """A regional node serves many people: whoever runs it is not every
    agent's owner there, even when HEVOLVE_OWNER_USER_ID names them (Nunba
    exports it on every tier)."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    cert = tmp_path / 'regional.cert'
    cert.write_text('cert')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
    monkeypatch.setenv('HEVOLVE_REGIONAL_CERT', str(cert))
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                scope={'can_voice': True})


@pytest.mark.parametrize('claimed_tier', ['regional', 'central', 'local'])
def test_a_node_that_claims_a_shared_tier_gives_the_machine_owner_nothing(
        fresh_db, monkeypatch, claimed_tier):
    """The tier a node claims is what withholds the right: Nunba's own
    promotion sets HEVOLVE_NODE_TIER=regional with no certificate, and a
    desktop upgraded to central keeps no key, and get_node_tier answers
    'flat' for both.  'local' (a node under a regional host) keeps the old
    rule too.  No key material is involved: the claim alone refuses."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', claimed_tier)
    monkeypatch.delenv('HEVOLVE_REGIONAL_CERT', raising=False)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                scope={'can_voice': True})
    grant = CallService.grant_agent(db, agent.id, author.id, 'community', com.id,
                                    scope={'can_voice': True})
    with pytest.raises(CallError):
        CallService.revoke_agent(db, grant['id'], me.id)


@pytest.mark.parametrize('claimed', [None, 'flat', ' Flat '])
def test_a_desktop_that_claims_no_shared_tier_keeps_the_machine_owners_right(
        fresh_db, monkeypatch, claimed):
    """A Nunba desktop sets no tier at all: unset is flat, as a claim
    written with spaces or capitals is."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    if claimed is None:
        monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)
    else:
        monkeypatch.setenv('HEVOLVE_NODE_TIER', claimed)
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                    scope={'can_voice': True})
    assert grant['owner_id'] == me.id


def test_the_machine_owner_changes_their_own_grant(fresh_db, monkeypatch):
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.call_service import CallService
    first = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                    scope={'can_voice': True})
    second = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                     scope={'can_voice': True, 'can_screen': True})
    assert second['id'] == first['id'] and second['owner_id'] == me.id
    assert second['scope'] == {'can_voice': True, 'can_screen': True}


def test_a_node_its_key_made_central_gives_the_machine_owner_nothing(
        fresh_db, monkeypatch):
    """Claiming nothing (flat) is not enough when the node has proven a
    shared tier: get_node_tier promotes a key-holding node to central.
    Stood in for here; no key material is involved."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                scope={'can_voice': True})


def test_the_author_cannot_take_back_a_grant_the_machine_owner_made(
        fresh_db, monkeypatch):
    """On the owner's machine the owner decides: once they set an agent's
    grant, its author changing it would make the owner's calls run, and be
    filed, as the author."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.call_service import CallService, CallError
    mine = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                   scope={'can_voice': True})
    with pytest.raises(CallError, match="machine's owner"):
        CallService.grant_agent(db, agent.id, author.id, 'community', com.id,
                                scope={'can_voice': True, 'can_screen': True})
    kept = CallService.get_active_grant(db, agent.id, 'community', com.id)
    assert kept['owner_id'] == me.id and kept['scope'] == {'can_voice': True}
    assert kept['id'] == mine['id']


def test_the_machine_owners_update_makes_the_grant_theirs(fresh_db, monkeypatch):
    """The author granted; the machine owner changes it: the grant is now
    the machine owner's, so the call it opens runs as them (the bridge is
    handed the grant's owner)."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.call_service import CallService
    first = CallService.grant_agent(db, agent.id, author.id, 'community', com.id,
                                    scope={'can_voice': False})
    assert first['owner_id'] == author.id
    second = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                     scope={'can_voice': True})
    assert second['id'] == first['id']
    assert second['owner_id'] == me.id and second['scope'] == {'can_voice': True}


def test_the_machine_owner_revokes_a_grant_someone_else_made(fresh_db, monkeypatch):
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    guest, = _seed_users(db, 1)
    from integrations.social.call_service import CallService, CallError
    grant = CallService.grant_agent(db, agent.id, author.id, 'community', com.id,
                                    scope={'can_voice': True})
    with pytest.raises(CallError):
        CallService.revoke_agent(db, grant['id'], guest.id)
    assert CallService.revoke_agent(db, grant['id'], me.id) is True
    assert CallService.get_active_grant(db, agent.id, 'community', com.id) is None


def test_the_machine_owner_cannot_grant_a_person(fresh_db, monkeypatch):
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError, match='not an agent'):
        CallService.grant_agent(db, author.id, me.id, 'community', com.id,
                                scope={'can_voice': True})


def test_a_machine_owner_written_with_spaces_is_still_the_owner(fresh_db, monkeypatch):
    """Read as every other owner gate reads it (capability_setup.setup_owner)."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', f' {me.id} ')
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                    scope={'can_voice': True})
    assert grant['owner_id'] == me.id


def test_this_machines_owner_grants_any_agent_on_it(fresh_db, monkeypatch):
    """Owner ruling 2026-10-08: an agent is shared as a recipe; whoever runs
    it on their own machine trusts it with their data, so that machine's
    owner decides whether it joins their calls, not its author."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                    scope={'can_voice': True})
    assert grant['owner_id'] == me.id and grant['scope'] == {'can_voice': True}


def test_this_machines_owner_grants_a_system_agent_on_it(fresh_db, monkeypatch):
    db, _ = fresh_db
    me, _author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    from integrations.social.models import User
    agent_row = db.query(User).filter(User.id == agent.id).first()
    agent_row.owner_id = None              # ownerless: made by an agent
    db.commit()
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(db, agent.id, me.id, 'community', com.id,
                                    scope={'can_voice': True})
    assert grant['agent_id'] == agent.id


def test_someone_else_on_the_machine_still_cannot_grant(fresh_db, monkeypatch):
    """Only the machine's owner gains the right: another person signed in on
    it is not the agent's owner and not the machine's."""
    db, _ = fresh_db
    me, author, agent, com = _machine_owner_and_stranger_agent(db, monkeypatch)
    guest, = _seed_users(db, 1)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.grant_agent(db, agent.id, guest.id, 'community', com.id,
                                scope={'can_voice': True})


def test_no_machine_owner_grants_nobody_the_machine_owners_right(fresh_db, monkeypatch):
    """Central and regional nodes set no HEVOLVE_OWNER_USER_ID: the unset
    owner matches no caller, not even one with no id."""
    db, _ = fresh_db
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    a, = _seed_users(db, 1)
    agent = _seed_agent(db, owner_id=None)
    com = _seed_community(db, owner_id=a.id)
    from integrations.social.call_service import CallService, CallError
    with pytest.raises(CallError):
        CallService.grant_agent(db, agent.id, '', 'community', com.id,
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

def test_token_falls_back_to_p2p_when_supervisor_disabled(monkeypatch):
    """Central / embedded deploys (LIVEKIT_DISABLE=1 or
    HEVOLVE_DEPLOY_MODE=central) do NOT host an SFU.  When env vars
    are unset AND the supervisor is disabled, issue_token returns
    mode='p2p_mesh' so clients run a WebRTC P2P mesh signaled over
    PeerLink instead of trying to connect to a non-existent SFU.

    (Flat/regional deploys auto-provision dev keys via the supervisor
    — see test_token_uses_supervisor_dev_keys_on_flat below.)"""
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    monkeypatch.delenv('LIVEKIT_API_KEY', raising=False)
    monkeypatch.delenv('LIVEKIT_API_SECRET', raising=False)
    monkeypatch.setenv('LIVEKIT_DISABLE', '1')
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


def test_token_uses_supervisor_dev_keys_on_flat(monkeypatch, tmp_path):
    """Flat/regional deploys auto-provision LiveKit dev keys via the
    supervisor.  With env vars unset, issue_token must reach a working
    config from the supervisor's auto-generated keys (no manual setup
    needed) — mode is 'livekit' (real JWT) or 'livekit_pending' (SDK
    not installed).  Dev keys + URL come from livekit_supervisor."""
    monkeypatch.delenv('LIVEKIT_URL', raising=False)
    monkeypatch.delenv('LIVEKIT_API_KEY', raising=False)
    monkeypatch.delenv('LIVEKIT_API_SECRET', raising=False)
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    monkeypatch.delenv('LIVEKIT_AUTOSTART', raising=False)
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    from integrations.social.livekit_service import LiveKitService
    r = LiveKitService.issue_token('call-1', 'user-1')
    # Either signed JWT (SDK installed) or pending shape (SDK absent)
    # — both confirm the supervisor's config flowed through.
    assert r['mode'] in ('livekit', 'livekit_pending')
    assert r.get('url', '').startswith('ws://localhost:')
    assert 'metadata' in r


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


# ── _decide_media_mode mesh-first promotion (Task #275) ─────────────
# Validates the "PeerLink ↔ LiveKit complement" branches.  Empirical
# threshold benchmarking is queued under #276; these tests just lock
# in the current contract so #276 can compare against a baseline.

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


def _patch_g_user(monkeypatch, uid='caller-1'):
    """Stub flask.g.user for _decide_media_mode caller-id resolution."""
    from integrations.social import api_calls
    class _G:
        user = _FakeUser(uid)
    monkeypatch.setattr(api_calls, 'g', _G)


def test_decide_media_mode_voice_under_threshold(monkeypatch):
    """≤4 active voice participants → p2p_mesh (PeerLink-signaled)."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    parts = [{'user_id': 'u1', 'left_at': None},
             {'user_id': 'u2', 'left_at': None}]
    assert _decide_media_mode(sess, parts, is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_voice_over_threshold(monkeypatch):
    """>4 active participants → livekit (mesh fanout becomes inefficient)."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    # 4 already in + caller about to join = 5 active → over threshold(=4)
    parts = [{'user_id': f'u{i}', 'left_at': None} for i in range(4)]
    assert _decide_media_mode(sess, parts, is_agent=False) == 'livekit'


def test_decide_media_mode_agent_always_livekit(monkeypatch):
    """Any agent participant → livekit (AgentVoiceBridge needs SFU)."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    assert _decide_media_mode(sess, [], is_agent=True) == 'livekit'


def test_decide_media_mode_a_person_calling_an_agent_gets_livekit(monkeypatch):
    """A person's token for a call an agent is in: livekit.  The agent is
    present through its AgentVoiceBridge, which has no media path but the
    SFU room -- on a mesh it can neither hear the person nor speak."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    parts = [{'user_id': 'agent-7', 'left_at': None, 'agent_kind': 'agent',
              'device_kind': 'agent_bridge'}]
    assert _decide_media_mode({'kind': 'voice'}, parts, is_agent=False) == 'livekit'
    # An agent that has left the call no longer holds it on the SFU.
    gone = [dict(parts[0], left_at='2026-10-07 10:00:00')]
    assert _decide_media_mode({'kind': 'voice'}, gone, is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_a_person_naming_their_device_agent_bridge_stays_on_the_mesh(monkeypatch):
    """device_kind is whatever a joining client says (join_call); only
    attach_agent writes agent_kind 'agent'.  A person who joins with
    device_kind 'agent_bridge' is not an agent and moves no one to LiveKit."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    parts = [{'user_id': 'person-9', 'left_at': None, 'agent_kind': 'human',
              'device_kind': 'agent_bridge'}]
    assert _decide_media_mode({'kind': 'voice'}, parts, is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_screen_share_always_livekit(monkeypatch):
    """screen_share / mixed kinds → livekit regardless of count."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    assert _decide_media_mode({'kind': 'screen_share'}, [],
                              is_agent=False) == 'livekit'
    assert _decide_media_mode({'kind': 'mixed'}, [],
                              is_agent=False) == 'livekit'


def test_decide_media_mode_threshold_override(monkeypatch):
    """LIVEKIT_MESH_THRESHOLD=999 keeps everything on mesh; =0 forces SFU."""
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    parts = [{'user_id': f'u{i}', 'left_at': None} for i in range(20)]
    monkeypatch.setenv('LIVEKIT_MESH_THRESHOLD', '999')
    assert _decide_media_mode(sess, parts, is_agent=False) == 'p2p_mesh'
    monkeypatch.setenv('LIVEKIT_MESH_THRESHOLD', '0')
    # Threshold clamped to min 1, so 1 person mesh; 2 → livekit.
    parts2 = [{'user_id': 'u1', 'left_at': None}]
    assert _decide_media_mode(sess, parts2, is_agent=False) == 'livekit'


def test_decide_media_mode_video_uses_tighter_default(monkeypatch):
    """Video crosses to SFU at N=4 (default 3) — one peer earlier than
    voice (default 4).  The bandwidth model justifies this: 500 kbps ×
    3 = 1.5 Mbps mesh upload at N=4, which saturates a typical
    residential uplink."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD_VIDEO', raising=False)
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD_VOICE', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    # 3 participants total (caller + 2 others) → at video threshold
    parts = [{'user_id': 'u1', 'left_at': None},
             {'user_id': 'u2', 'left_at': None}]
    assert _decide_media_mode({'kind': 'video'}, parts,
                              is_agent=False) == 'p2p_mesh'
    # 4 participants → over video threshold (3)
    parts4 = parts + [{'user_id': 'u3', 'left_at': None}]
    assert _decide_media_mode({'kind': 'video'}, parts4,
                              is_agent=False) == 'livekit'
    # Same N=4 on a voice call still mesh (voice threshold = 4)
    assert _decide_media_mode({'kind': 'voice'}, parts4,
                              is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_per_kind_env_override(monkeypatch):
    """LIVEKIT_MESH_THRESHOLD_VOICE / _VIDEO override per-kind."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    monkeypatch.setenv('LIVEKIT_MESH_THRESHOLD_VIDEO', '6')
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    # 5 video participants — would default to SFU (>3), but per-kind
    # override allows mesh up to 6.
    parts = [{'user_id': f'u{i}', 'left_at': None} for i in range(4)]
    assert _decide_media_mode({'kind': 'video'}, parts,
                              is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_uniform_env_overrides_per_kind(monkeypatch):
    """LIVEKIT_MESH_THRESHOLD (uniform) takes precedence over the
    per-kind default but loses to the per-kind override."""
    monkeypatch.setenv('LIVEKIT_MESH_THRESHOLD', '2')
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD_VOICE', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    # 3 voice participants (caller + 2) — over uniform threshold of 2
    parts = [{'user_id': 'u1', 'left_at': None},
             {'user_id': 'u2', 'left_at': None}]
    assert _decide_media_mode({'kind': 'voice'}, parts,
                              is_agent=False) == 'livekit'


def test_mesh_threshold_unknown_kind_falls_back(monkeypatch):
    """An unknown kind (not in _DEFAULT_KIND_THRESHOLDS) falls back to
    the conservative 4 — no KeyError, no NaN."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    from integrations.social.api_calls import _mesh_threshold
    assert _mesh_threshold('weird_unknown_kind') == 4


def test_mesh_threshold_garbage_env_falls_back(monkeypatch):
    """Non-numeric env values fall back to the default per-kind."""
    monkeypatch.setenv('LIVEKIT_MESH_THRESHOLD', 'not-a-number')
    from integrations.social.api_calls import _mesh_threshold
    # voice default is 4 from _DEFAULT_KIND_THRESHOLDS
    assert _mesh_threshold('voice') == 4


def test_bandwidth_model_crossover_table():
    """Sanity-check the bandwidth model: mesh upload grows linearly,
    SFU upload stays flat."""
    from integrations.social._mesh_bandwidth_model import crossover_table
    table = crossover_table('video', max_n=5)
    assert table[2].mesh_up_kbps == 500   # 1 peer × 500 kbps
    assert table[3].mesh_up_kbps == 1000
    assert table[4].mesh_up_kbps == 1500
    assert table[5].mesh_up_kbps == 2000
    # SFU upload stays flat at one stream regardless of N
    for n in range(2, 6):
        assert table[n].sfu_up_kbps == 500


def test_bandwidth_model_first_n_above_ceiling():
    """1500 kbps uplink with VP8 video — mesh fits through N=4
    (1500 kbps mesh upload), exceeds at N=5."""
    from integrations.social._mesh_bandwidth_model import (
        first_n_where_mesh_upload_exceeds,
    )
    # video at N=4 = 1500 kbps == ceiling (not >), so first exceeding N
    # is 5.
    assert first_n_where_mesh_upload_exceeds('video', 1500) == 5
    # voice is so cheap it never exceeds even at N=46 (45 × 32 = 1440)
    # but at N=47 it's 1472, still under.  At N=48: 1504 — exceeds.
    assert first_n_where_mesh_upload_exceeds('voice', 1500) == 48


def test_operational_thresholds_is_single_source():
    """The bandwidth-model module owns OPERATIONAL_THRESHOLDS;
    api_calls imports it as _DEFAULT_KIND_THRESHOLDS.  This test
    asserts the SAME-OBJECT identity, which guards against someone
    re-introducing a parallel literal in api_calls.py."""
    from integrations.social._mesh_bandwidth_model import (
        OPERATIONAL_THRESHOLDS,
    )
    from integrations.social.api_calls import _DEFAULT_KIND_THRESHOLDS
    # `is` check — equality would still pass with two parallel dicts
    # that happen to match.  Identity ensures one source of truth.
    assert _DEFAULT_KIND_THRESHOLDS is OPERATIONAL_THRESHOLDS


def test_supervisor_binary_url_uses_underscore_separator(monkeypatch):
    """LiveKit's release filenames use `linux_amd64`, not `linux-amd64`.
    Our internal platform tag uses hyphen (doubles as dict key) — the
    URL builder must translate hyphen → underscore."""
    monkeypatch.delenv('LIVEKIT_BINARY_URL', raising=False)
    from integrations.social import livekit_supervisor
    # Pin platform.system / machine for deterministic URL.
    monkeypatch.setattr(livekit_supervisor.platform, 'system',
                        lambda: 'Linux')
    monkeypatch.setattr(livekit_supervisor.platform, 'machine',
                        lambda: 'x86_64')
    url = livekit_supervisor._binary_url()
    assert 'livekit_1.7.2_linux_amd64.tar.gz' in url
    assert 'linux-amd64' not in url


def test_supervisor_binary_url_windows_uses_zip(monkeypatch):
    monkeypatch.delenv('LIVEKIT_BINARY_URL', raising=False)
    from integrations.social import livekit_supervisor
    monkeypatch.setattr(livekit_supervisor.platform, 'system',
                        lambda: 'Windows')
    monkeypatch.setattr(livekit_supervisor.platform, 'machine',
                        lambda: 'AMD64')
    url = livekit_supervisor._binary_url()
    assert url.endswith('windows_amd64.zip')


def test_supervisor_binary_url_override_env(monkeypatch):
    """LIVEKIT_BINARY_URL bypasses URL construction (air-gapped /
    mirror / file:// builds)."""
    monkeypatch.setenv('LIVEKIT_BINARY_URL',
                       'file:///cache/livekit-1.7.2.tar.gz')
    from integrations.social import livekit_supervisor
    assert livekit_supervisor._binary_url() == 'file:///cache/livekit-1.7.2.tar.gz'


def test_supervisor_sha256_pinned_for_supported_platforms():
    """Supply-chain integrity: every platform we build a download URL
    for must have a non-empty SHA-256 pin.  Adding a new platform tag
    without pinning its hash is a CI-breaking mistake."""
    from integrations.social.livekit_supervisor import _LIVEKIT_SHA256
    expected_keys = {
        'linux-amd64', 'linux-arm64', 'linux-armv7',
        'windows-amd64', 'windows-arm64', 'windows-armv7',
    }
    for key in expected_keys:
        assert key in _LIVEKIT_SHA256, f'missing SHA-256 pin for {key}'
        # Real SHA-256 hex is 64 chars; empty would skip verification.
        assert len(_LIVEKIT_SHA256[key]) == 64, (
            f'SHA-256 pin for {key} is empty or wrong length')


# ── Bind-address policy: silent install on flat / no firewall prompt ─

def test_bind_addresses_flat_mode_loopback(monkeypatch):
    """Flat / embedded → loopback only so first start doesn't trigger
    the Windows / macOS firewall prompt."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.delenv('LIVEKIT_BIND_HOST', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor._bind_addresses_for_mode() == ['127.0.0.1']
    # External IP advertisement is meaningless on loopback — must be off.
    assert livekit_supervisor._use_external_ip_for_mode() is False


def test_bind_addresses_regional_mode_all_interfaces(monkeypatch):
    """Regional hosts SFU for LAN peers — bind all interfaces.  The
    one-time firewall prompt is acceptable; the operator chose regional."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'regional')
    monkeypatch.delenv('LIVEKIT_BIND_HOST', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor._bind_addresses_for_mode() == ['']
    assert livekit_supervisor._use_external_ip_for_mode() is True


def test_bind_addresses_env_override_specific_nic(monkeypatch):
    """LIVEKIT_BIND_HOST=<addr> wins over the mode-aware default."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')  # would be loopback
    monkeypatch.setenv('LIVEKIT_BIND_HOST', '192.168.1.50')
    from integrations.social import livekit_supervisor
    assert livekit_supervisor._bind_addresses_for_mode() == ['192.168.1.50']
    # Non-loopback bind → advertise external IP.
    assert livekit_supervisor._use_external_ip_for_mode() is True


def test_bind_addresses_env_override_zero_host_means_all_interfaces(
        monkeypatch):
    """0.0.0.0 is the conventional 'all interfaces' literal — translate
    to LiveKit's empty-string sentinel."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.setenv('LIVEKIT_BIND_HOST', '0.0.0.0')
    from integrations.social import livekit_supervisor
    assert livekit_supervisor._bind_addresses_for_mode() == ['']
    assert livekit_supervisor._use_external_ip_for_mode() is True


def test_generated_config_writes_loopback_for_flat(monkeypatch, tmp_path):
    """End-to-end: _generate_config emits 'bind_addresses: - 127.0.0.1'
    for flat mode so the first start is silent."""
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.delenv('LIVEKIT_BIND_HOST', raising=False)
    from integrations.social import livekit_supervisor
    cfg_path = livekit_supervisor._generate_config(
        {'api_key': 'KFLAT', 'api_secret': 'SFLAT' * 8})
    body = cfg_path.read_text(encoding='utf-8')
    assert "bind_addresses:" in body
    assert "  - '127.0.0.1'" in body
    # 0.0.0.0 / empty must NOT be present on a flat deploy.
    assert "  - ''" not in body
    assert "use_external_ip: false" in body


def test_generated_config_writes_all_interfaces_for_regional(
        monkeypatch, tmp_path):
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'regional')
    monkeypatch.delenv('LIVEKIT_BIND_HOST', raising=False)
    from integrations.social import livekit_supervisor
    cfg_path = livekit_supervisor._generate_config(
        {'api_key': 'KREG', 'api_secret': 'SREG' * 8})
    body = cfg_path.read_text(encoding='utf-8')
    assert "  - ''" in body
    assert "use_external_ip: true" in body


# ── AgentVoiceBridge TTS outbox (Task #219 magic loop) ──────────────

@pytest.fixture
def clean_bridge_outbox():
    """Reset module-level TTS outbox between tests so they don't see
    stale state from prior tests."""
    from integrations.social import agent_voice_bridge as avb
    with avb._TTS_LOCK:
        avb._TTS_OUTBOX.clear()
    yield
    with avb._TTS_LOCK:
        avb._TTS_OUTBOX.clear()


def test_tts_outbox_enqueue_dequeue_roundtrip(clean_bridge_outbox):
    """Push then pop returns the same text in FIFO order."""
    from integrations.social.agent_voice_bridge import (
        enqueue_tts_text, dequeue_tts_text)
    assert enqueue_tts_text('call-1', 'agent-1', 'hello there') is True
    assert enqueue_tts_text('call-1', 'agent-1', 'second line') is True
    out = dequeue_tts_text('call-1', 'agent-1')
    assert [r['text'] for r in out] == ['hello there', 'second line']
    # All consumed — second drain returns empty.
    assert dequeue_tts_text('call-1', 'agent-1') == []


def test_tts_outbox_enqueue_rejects_empty(clean_bridge_outbox):
    """Empty / whitespace text is silently dropped (False return)."""
    from integrations.social.agent_voice_bridge import (
        enqueue_tts_text, tts_outbox_depth)
    assert enqueue_tts_text('call-1', 'agent-1', '') is False
    assert enqueue_tts_text('call-1', 'agent-1', '   ') is False
    assert enqueue_tts_text('', 'agent-1', 'hi') is False
    assert enqueue_tts_text('call-1', '', 'hi') is False
    assert tts_outbox_depth('call-1', 'agent-1') == 0


def test_tts_outbox_per_agent_isolation(clean_bridge_outbox):
    """Multiple agents in the same call have independent queues."""
    from integrations.social.agent_voice_bridge import (
        enqueue_tts_text, dequeue_tts_text, tts_outbox_depth)
    enqueue_tts_text('call-1', 'agent-A', 'A says hi')
    enqueue_tts_text('call-1', 'agent-B', 'B says hello')
    assert tts_outbox_depth('call-1', 'agent-A') == 1
    assert tts_outbox_depth('call-1', 'agent-B') == 1
    out_a = dequeue_tts_text('call-1', 'agent-A')
    assert [r['text'] for r in out_a] == ['A says hi']
    # agent-B's queue is untouched
    assert tts_outbox_depth('call-1', 'agent-B') == 1


def test_tts_outbox_cap_evicts_oldest(clean_bridge_outbox, monkeypatch):
    """Cap at _TTS_OUTBOX_CAP — oldest dropped with WARN."""
    from integrations.social import agent_voice_bridge as avb
    monkeypatch.setattr(avb, '_TTS_OUTBOX_CAP', 3)
    for i in range(5):
        avb.enqueue_tts_text('call-1', 'agent-1', f'line {i}')
    out = avb.dequeue_tts_text('call-1', 'agent-1', limit=10)
    # Cap is 3 — oldest 2 dropped.  Remaining is FIFO of last 3.
    assert [r['text'] for r in out] == ['line 2', 'line 3', 'line 4']


def test_tts_outbox_dequeue_limit(clean_bridge_outbox):
    """dequeue with limit=N returns at most N items, leaves rest."""
    from integrations.social.agent_voice_bridge import (
        enqueue_tts_text, dequeue_tts_text, tts_outbox_depth)
    for i in range(5):
        enqueue_tts_text('call-1', 'agent-1', f'line {i}')
    out = dequeue_tts_text('call-1', 'agent-1', limit=2)
    assert [r['text'] for r in out] == ['line 0', 'line 1']
    assert tts_outbox_depth('call-1', 'agent-1') == 3


def test_detach_agent_clears_tts_outbox(clean_bridge_outbox):
    """detach_agent must drop pending TTS chunks so a later same-key
    attach doesn't replay stale audio."""
    from integrations.social.agent_voice_bridge import (
        enqueue_tts_text, dequeue_tts_text, AgentVoiceBridge)
    enqueue_tts_text('call-1', 'agent-1', 'pending audio')
    # No worker actually exists for this test — detach returns False
    # but should still scrub the outbox.
    AgentVoiceBridge.detach_agent('call-1', 'agent-1')
    assert dequeue_tts_text('call-1', 'agent-1') == []


class _StubDBOnlyAgent:
    """Minimal `db_session()` stub: User.query→agent succeeds; everything
    else returns empty.  Reused by source_kind branch tests that only
    need the agent lookup to succeed before exercising side-effect
    delegation.  Mirrors the inline class in
    test_router_source_kind_call_enqueues_tts (which kept it inline for
    historical reasons; this top-level version is the single canonical
    home for new callers)."""

    def __init__(self, agent_id='agent-99'):
        self._agent_id = agent_id

    class _Q:
        def __init__(self, agent):
            self.agent = agent

        def filter(self, *args, **kw):
            return self

        def first(self):
            return self.agent

    def query(self, model):
        from integrations.social.models import User
        if model is User:
            agent = type('A', (), {'id': self._agent_id})()
            return _StubDBOnlyAgent._Q(agent)
        return _StubDBOnlyAgent._Q(None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        class _R:
            def fetchone(self_):
                return None
        return _R()


def test_router_source_kind_external_room_delegates_to_response_router(
        monkeypatch):
    """source_kind='external_room' must hand off to the canonical
    ChannelResponseRouter.route_response — no parallel outbound path
    invented; just the one branch that delegates.  Verifies:
      - the canonical router is called
      - channel_context flows through unchanged (channel + chat_id)
      - agent_id is forwarded so logging attributes the assistant turn
      - default fan_out=False so the reply lands in the originating
        room only (caller can flip via context['fan_out_external'])
    """
    from integrations import agentic_router
    from integrations.channels.response import router as response_router_mod
    from integrations.social import models as _models

    monkeypatch.setattr(
        _models, 'db_session',
        lambda: _StubDBOnlyAgent(agent_id='agent-ext-1'))

    captured = {}

    class _StubResponseRouter:
        def route_response(self, user_id, response_text,
                           channel_context=None, agent_id=None,
                           fan_out=True):
            captured.update({
                'user_id': user_id,
                'response_text': response_text,
                'channel_context': channel_context,
                'agent_id': agent_id,
                'fan_out': fan_out,
            })

    monkeypatch.setattr(
        response_router_mod, 'get_response_router',
        lambda registry=None: _StubResponseRouter())

    agentic_router._post_agent_reply(
        agent_id='agent-ext-1',
        context={
            'source_kind': 'external_room',
            'source_id': 'conversation-entry-id-42',
            'owner_id': 'user-7',
            'channel_context': {
                'channel': 'discord',
                'chat_id': '987654321',
                'sender_id': 'discord:user-99',
                'sender_name': 'Aru',
                'is_group': True,
                'message_id': 'discord-msg-555',
            },
        },
        reply_text='Hi from the agent — answering on Discord.',
    )

    assert captured, (
        "ChannelResponseRouter.route_response was never called — the "
        "external_room branch did not delegate to the canonical "
        "outbound surface"
    )
    assert captured['response_text'].startswith('Hi from the agent')
    assert captured['user_id'] == 'user-7'
    assert captured['agent_id'] == 'agent-ext-1'
    assert captured['channel_context']['channel'] == 'discord'
    assert captured['channel_context']['chat_id'] == '987654321'
    # Default fan_out is False — agent reply stays in the originating
    # room unless the caller explicitly opts in.
    assert captured['fan_out'] is False


def test_router_source_kind_external_room_skips_when_context_missing(
        monkeypatch):
    """Defensive: if channel_context is empty / missing channel + chat_id,
    the branch logs and returns rather than calling the router with bad
    args.  Prevents adapter-misconfiguration from cascading into
    ChannelRegistry exceptions."""
    from integrations import agentic_router
    from integrations.channels.response import router as response_router_mod
    from integrations.social import models as _models

    monkeypatch.setattr(
        _models, 'db_session',
        lambda: _StubDBOnlyAgent(agent_id='agent-ext-2'))

    called = {'count': 0}

    class _StubResponseRouter:
        def route_response(self, **kwargs):
            called['count'] += 1

    monkeypatch.setattr(
        response_router_mod, 'get_response_router',
        lambda registry=None: _StubResponseRouter())

    # No channel_context at all
    agentic_router._post_agent_reply(
        agent_id='agent-ext-2',
        context={'source_kind': 'external_room',
                 'source_id': 'ce-id'},
        reply_text='should not be sent',
    )
    assert called['count'] == 0

    # channel_context missing chat_id
    agentic_router._post_agent_reply(
        agent_id='agent-ext-2',
        context={'source_kind': 'external_room',
                 'source_id': 'ce-id',
                 'channel_context': {'channel': 'discord'}},
        reply_text='should not be sent either',
    )
    assert called['count'] == 0


def test_router_source_kind_external_room_fan_out_opt_in(monkeypatch):
    """Caller opts into bound-channel fan-out via
    context['fan_out_external']=True — passes through to route_response
    so the same reply goes to every bound channel for the user."""
    from integrations import agentic_router
    from integrations.channels.response import router as response_router_mod
    from integrations.social import models as _models

    monkeypatch.setattr(
        _models, 'db_session',
        lambda: _StubDBOnlyAgent(agent_id='agent-ext-3'))

    captured = {}

    class _StubResponseRouter:
        def route_response(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        response_router_mod, 'get_response_router',
        lambda registry=None: _StubResponseRouter())

    agentic_router._post_agent_reply(
        agent_id='agent-ext-3',
        context={
            'source_kind': 'external_room',
            'source_id': 'ce-id',
            'channel_context': {'channel': 'whatsapp', 'chat_id': '+1555'},
            'fan_out_external': True,
        },
        reply_text='broadcast me everywhere',
    )

    assert captured.get('fan_out') is True


def test_router_source_kind_call_enqueues_tts(clean_bridge_outbox,
                                               monkeypatch):
    """The new branch in agentic_router._post_agent_reply must push
    the agent's reply onto the TTS outbox so the bridge worker can
    drain it on its next tick.

    We monkeypatch the DB session lookup so we don't need a real
    SQLAlchemy session for this branch test."""
    from integrations.social.agent_voice_bridge import (
        dequeue_tts_text, tts_outbox_depth)
    from integrations import agentic_router

    # Stub the DB context manager + User/Post/Comment lookups — only
    # the agent lookup needs to succeed for the 'call' branch.
    class _StubDB:
        class _Q:
            def __init__(self, agent):
                self.agent = agent
            def filter(self, *args, **kw):
                return self
            def first(self):
                return self.agent
        def query(self, model):
            from integrations.social.models import User
            if model is User:
                stub_agent = type('A', (), {'id': 'agent-99'})()
                return _StubDB._Q(stub_agent)
            return _StubDB._Q(None)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **kw):
            class _R:
                def fetchone(self_):
                    return None
            return _R()

    def _stub_db_session():
        return _StubDB()

    # Patch where the symbol is looked up — agentic_router does
    # `from integrations.social.models import db_session, User, ...`
    # inside the function, so we patch the source module.
    from integrations.social import models as _models
    monkeypatch.setattr(_models, 'db_session', _stub_db_session)

    agentic_router._post_agent_reply(
        agent_id='agent-99',
        context={'source_kind': 'call', 'source_id': 'call-X',
                 'platform': 'livekit', 'owner_id': 'owner-1'},
        reply_text='hello from the agent',
    )

    out = dequeue_tts_text('call-X', 'agent-99')
    assert len(out) == 1
    assert out[0]['text'] == 'hello from the agent'
    # outbox is now drained
    assert tts_outbox_depth('call-X', 'agent-99') == 0


# ── A call turn is a turn of the agent: its own prompt through /chat ──────


def _call_turn_rig(db, monkeypatch, chat_status=200, chat_body=None,
                   trained_id='54_0', prompt_file=True, flows=2, flows_built=2,
                   prompt_name='54'):
    """A trained agent (agent_bridge.sync_trained_agents names its row
    'agent_{prompt_id}_{flow_id}' and keeps '{prompt_id}_{flow_id}' as its
    agent_id; one a person built keeps the bare prompt_id), its prompt and
    recipes on this node (prompt 54, ``flows`` flows of which the first
    ``flows_built`` have a recipe), the local /chat it reaches, and the plain
    model it must not need.  Returns (agent, speaker, posts,
    plain_model_calls, minted)."""
    import json as _json
    import tempfile
    from integrations.social.models import User
    prompts = tempfile.mkdtemp(prefix='call_prompts_')
    if prompt_file:
        with open(os.path.join(prompts, f'{prompt_name}.json'), 'w') as f:
            _json.dump({'flows': [{'flow_name': f'f{n}'} for n in range(flows)]}, f)
    for n in range(flows_built):
        with open(os.path.join(prompts, f'{prompt_name}_{n}_recipe.json'), 'w') as f:
            _json.dump({'recipe': []}, f)
    import core.platform_paths
    monkeypatch.setattr(core.platform_paths, 'get_recipe_prompts_dir', lambda: prompts)
    speaker, = _seed_users(db, 1)
    agent = User(id=str(uuid.uuid4()), username=f'agent_{uuid.uuid4().hex[:6]}',
                 display_name='Tutor', email=f'a_{uuid.uuid4().hex[:6]}@x.test',
                 password_hash='x:y', user_type='agent', agent_id=trained_id)
    db.add(agent)
    db.commit()
    posts, plain, minted = [], [], []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append({'url': url, 'json': json, 'headers': headers,
                      'timeout': timeout})
        from unittest.mock import Mock
        return Mock(status_code=chat_status,
                    json=lambda: (chat_body if chat_body is not None
                                  else {'response': 'Halves are two equal parts.'}),
                    text='')

    class _PlainModel:
        def invoke(self, prompt):
            plain.append(prompt)
            return type('R', (), {'content': 'a plain answer'})()

    def fake_mint(user_id='system_daemon', username=None, role='admin'):
        minted.append({'user_id': user_id, 'role': role})
        return {'Authorization': 'Bearer t'}

    import core.http_pool
    import core.port_registry
    import core.safe_hartos_attr
    from integrations.agent_engine import dispatch
    from integrations.social import chat_messages
    # The reply's transcript row is written by chat_messages' own background
    # thread; left running it writes into this test's database while the
    # fixture disposes it (an access violation in sqlite).  Not this test's
    # concern: recorded, not run.
    monkeypatch.setattr(chat_messages, 'persist_external_room_event',
                        lambda **kw: None)
    monkeypatch.setattr(core.http_pool, 'pooled_post', fake_post)
    monkeypatch.setattr(core.port_registry, 'get_local_backend_url',
                        lambda: 'http://this-node')
    monkeypatch.setattr(dispatch, '_internal_auth_headers', fake_mint)
    monkeypatch.setattr(core.safe_hartos_attr, 'safe_hartos_attr',
                        lambda name: (lambda **kw: _PlainModel())
                        if name == 'get_llm' else None)
    monkeypatch.delenv('HEVOLVE_FLAG_DISPATCH_VIA_CHAT', raising=False)
    return agent, speaker, posts, plain, minted


def _speak_in_call(agent, speaker, words='teach me fractions', call_id='call-7',
                   author_id=None):
    from integrations import agentic_router
    agentic_router.dispatch_to_agent(
        agent_id=agent.id, prompt=words, synchronous=True,
        context={'source_kind': 'call', 'source_id': call_id,
                 'author_id': author_id or speaker.id,
                 'owner_id': 'whoever-granted-it', 'platform': 'livekit'})


def _answered_by_the_plain_model_and_said_so(agent, posts_expected, posts, plain,
                                             caplog):
    assert len(posts) == posts_expected
    assert plain == ['teach me fractions']
    assert any(agent.id in r.getMessage() and r.levelname == 'WARNING'
               for r in caplog.records)


def test_a_call_turn_runs_the_agents_own_prompt_through_chat(
        fresh_db, clean_bridge_outbox, monkeypatch):
    """Owner rule: a turn from the phone is agentic (CREATE/REUSE).  The
    agent answers as itself -- /chat with its prompt_id, as the person who
    spoke -- and its reply is what the call hears; the bridge speaks it, so
    /chat is asked for text only (media_mode='text': no second voice)."""
    db, _ = fresh_db
    agent, speaker, posts, plain, minted = _call_turn_rig(db, monkeypatch)
    _speak_in_call(agent, speaker)

    assert len(posts) == 1, 'the call turn reached the local /chat once'
    sent = posts[0]
    assert sent['url'] == 'http://this-node/chat'
    body = sent['json']
    assert str(body['prompt_id']) == '54'
    assert body['user_id'] == speaker.id
    assert body['prompt'] == body['text'] == 'teach me fractions'
    assert body['create_agent'] is False
    assert body['media_mode'] == 'text'
    assert body['channel_context'] == {'source_kind': 'call',
                                       'source_id': 'call-7'}
    # Someone speaking is a person's turn: it takes the model ahead of the
    # daemons, and they yield to it (the one discriminator /chat applies).
    from integrations.agent_engine.dispatch import is_genuine_user_request
    assert is_genuine_user_request(body['request_id'])
    assert minted == [{'user_id': speaker.id, 'role': 'user'}]
    assert sent['headers'] == {'Authorization': 'Bearer t'}
    assert plain == [], 'the plain model never answered for the agent'
    from integrations.social.agent_voice_bridge import dequeue_tts_text
    spoken = [r['text'] for r in dequeue_tts_text('call-7', agent.id)]
    assert spoken == ['Halves are two equal parts.']


def test_the_bundled_desktops_chat_answer_is_read_too(
        fresh_db, clean_bridge_outbox, monkeypatch):
    """On a bundled desktop Nunba's /chat answers, under 'text'."""
    db, _ = fresh_db
    agent, speaker, posts, plain, _ = _call_turn_rig(
        db, monkeypatch, chat_body={'text': 'From the desktop.'})
    _speak_in_call(agent, speaker)
    from integrations.social.agent_voice_bridge import dequeue_tts_text
    assert [r['text'] for r in dequeue_tts_text('call-7', agent.id)] == [
        'From the desktop.']
    assert plain == []


def test_an_agent_a_person_built_keeps_its_bare_prompt_id_and_gets_its_turn(
        fresh_db, clean_bridge_outbox, monkeypatch):
    """hart_intelligence_entry._create_social_agent_from_prompt registers a
    person's agent with agent_id=str(prompt_id): '54', not '54_0'."""
    db, _ = fresh_db
    agent, speaker, posts, plain, _ = _call_turn_rig(db, monkeypatch, trained_id='54')
    _speak_in_call(agent, speaker)
    assert len(posts) == 1 and str(posts[0]['json']['prompt_id']) == '54'
    assert plain == []


@pytest.mark.parametrize('trained_id', [None, '', 'skills_agent', 'x_y', 'abc_0'])
def test_an_agent_with_no_trained_prompt_is_answered_by_the_plain_model_and_says_so(
        fresh_db, clean_bridge_outbox, monkeypatch, caplog, trained_id):
    db, _ = fresh_db
    agent, speaker, posts, plain, _ = _call_turn_rig(
        db, monkeypatch, trained_id=trained_id)
    with caplog.at_level('WARNING'):
        _speak_in_call(agent, speaker)
    _answered_by_the_plain_model_and_said_so(agent, 0, posts, plain, caplog)


def test_an_agent_whose_prompt_id_is_not_a_number_gets_no_turn_through_chat(
        fresh_db, clean_bridge_outbox, monkeypatch, caplog):
    """Complete here, but its prompt_id is a UUID: Nunba's /chat (a bundled
    desktop) would run it as its default agent, answering in its name."""
    db, _ = fresh_db
    uid = 'f3a9c2d1-0b7e-4c55-9a21-6d0f2e8b1c44'
    agent, speaker, posts, plain, _ = _call_turn_rig(
        db, monkeypatch, trained_id=f'{uid}_0', prompt_name=uid)
    with caplog.at_level('WARNING'):
        _speak_in_call(agent, speaker)
    _answered_by_the_plain_model_and_said_so(agent, 0, posts, plain, caplog)


@pytest.mark.parametrize('prompt_file,flows_built', [
    # No prompt here: /chat would start gathering a new agent, and Nunba's
    # /chat would send the words to the cloud.
    (False, 0),
    # A flow still unbuilt: /chat would resume building the agent.
    (True, 1),
])
def test_an_agent_not_complete_on_this_node_gets_no_turn_through_chat(
        fresh_db, clean_bridge_outbox, monkeypatch, caplog, prompt_file, flows_built):
    """Words in a call never build an agent, and never leave the node: only
    an agent whose every flow has its recipe here (/chat's REUSE condition)
    is asked."""
    db, _ = fresh_db
    agent, speaker, posts, plain, _ = _call_turn_rig(
        db, monkeypatch, prompt_file=prompt_file, flows_built=flows_built)
    with caplog.at_level('WARNING'):
        _speak_in_call(agent, speaker)
    _answered_by_the_plain_model_and_said_so(agent, 0, posts, plain, caplog)


@pytest.mark.parametrize('author', ['unknown', 'PA_xK2j9', 'agent'])
def test_words_from_no_person_here_run_as_nobody(
        fresh_db, clean_bridge_outbox, monkeypatch, caplog, author):
    """A turn runs as the person who spoke.  An author who is no person here
    -- an unattributed speaker, a LiveKit session id, another agent -- is
    never minted a token: every such speaker would share one agent session."""
    db, _ = fresh_db
    agent, speaker, posts, plain, minted = _call_turn_rig(db, monkeypatch)
    with caplog.at_level('WARNING'):
        _speak_in_call(agent, speaker,
                       author_id=agent.id if author == 'agent' else author)
    _answered_by_the_plain_model_and_said_so(agent, 0, posts, plain, caplog)
    assert minted == []


def _hartos_generic_error():
    from core.constants import LLM_GENERIC_ERROR_REPLY
    return {'response': LLM_GENERIC_ERROR_REPLY}


@pytest.mark.parametrize('status,body', [
    (503, {'error': 'busy'}),
    # A refusal is not the agent's answer, whatever key carries its words.
    (503, {'response': 'Your local AI is busy with another task right now.'}),
    (200, {'response': '   '}),
    # What /chat really answers with when it did not run the agent, as
    # HTTP 200: Nunba's busy and starting notices, its refusal, the adapter's
    # still-loading notice, and HARTOS's own failure sentence.
    (200, {'text': 'Your local AI is busy with another task right now. '
                   'Send your message again in a moment.',
           'error': 'local_llm_starting', 'llm_starting': True, 'success': False}),
    (200, {'text': 'Starting the local AI engine for you now.',
           'error': 'local_llm_starting', 'success': False}),
    (200, {'text': 'Request could not be processed: blocked',
           'error': 'blocked', 'success': False}),
    (200, {'text': 'Still waking up.', 'loading': True, 'source': 'hartos_loading'}),
    (200, 'hartos-generic-error'),
    # Nunba answering for itself while no model is loaded: its setup card.
    (200, {'text': 'Setting up Qwen3.5-4B... Click below to start.',
           'agent_type': 'local', 'source': 'system', 'success': True,
           'llm_setup_card': {'model_type': 'llm'}}),
    # Any answer that says it failed, with or without an error code.
    (200, {'text': 'That did not work.', 'success': False}),
])
def test_a_call_turn_chat_cannot_answer_falls_to_the_plain_model_and_says_so(
        fresh_db, clean_bridge_outbox, monkeypatch, caplog, status, body):
    db, _ = fresh_db
    if body == 'hartos-generic-error':
        body = _hartos_generic_error()
    agent, speaker, posts, plain, _ = _call_turn_rig(
        db, monkeypatch, chat_status=status, chat_body=body)
    with caplog.at_level('WARNING'):
        _speak_in_call(agent, speaker)
    _answered_by_the_plain_model_and_said_so(agent, 1, posts, plain, caplog)
    from integrations.social.agent_voice_bridge import dequeue_tts_text
    assert [r['text'] for r in dequeue_tts_text('call-7', agent.id)] == [
        'a plain answer']


def test_decide_media_mode_excludes_left_participants(monkeypatch):
    """Active count uses left_at IS NULL — left rows don't count."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch)
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    # 5 entries but 3 left → 2 active + 1 caller = 3 → mesh
    parts = [{'user_id': 'u1', 'left_at': None},
             {'user_id': 'u2', 'left_at': None},
             {'user_id': 'u3', 'left_at': '2026-05-07T10:00:00Z'},
             {'user_id': 'u4', 'left_at': '2026-05-07T10:01:00Z'},
             {'user_id': 'u5', 'left_at': '2026-05-07T10:02:00Z'}]
    assert _decide_media_mode(sess, parts, is_agent=False) == 'p2p_mesh'


def test_decide_media_mode_caller_already_active_no_double_count(monkeypatch):
    """If caller is already in the participant list, don't add +1."""
    monkeypatch.delenv('LIVEKIT_MESH_THRESHOLD', raising=False)
    _patch_g_user(monkeypatch, uid='caller-1')
    from integrations.social.api_calls import _decide_media_mode
    sess = {'kind': 'voice'}
    # 4 active including caller → at threshold (4), not over
    parts = [{'user_id': 'caller-1', 'left_at': None},
             {'user_id': 'u2', 'left_at': None},
             {'user_id': 'u3', 'left_at': None},
             {'user_id': 'u4', 'left_at': None}]
    assert _decide_media_mode(sess, parts, is_agent=False) == 'p2p_mesh'


# ── Supervisor branch tests (Task #275) ─────────────────────────────

def test_supervisor_should_run_central_returns_false(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'central')
    monkeypatch.delenv('LIVEKIT_AUTOSTART', raising=False)
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor.supervisor_should_run() is False


def test_supervisor_should_run_flat_returns_true(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.delenv('LIVEKIT_AUTOSTART', raising=False)
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor.supervisor_should_run() is True


def test_supervisor_disable_env_overrides_mode(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.setenv('LIVEKIT_DISABLE', '1')
    monkeypatch.delenv('LIVEKIT_AUTOSTART', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor.supervisor_should_run() is False


def test_supervisor_autostart_env_overrides_mode(monkeypatch):
    """LIVEKIT_AUTOSTART=1 forces supervisor on regardless of deploy mode."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'central')
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '1')
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor.supervisor_should_run() is True


def test_supervisor_autostart_zero_overrides_mode(monkeypatch):
    """LIVEKIT_AUTOSTART=0 forces supervisor off regardless of deploy mode."""
    monkeypatch.setenv('HEVOLVE_DEPLOY_MODE', 'flat')
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '0')
    monkeypatch.delenv('LIVEKIT_DISABLE', raising=False)
    from integrations.social import livekit_supervisor
    assert livekit_supervisor.supervisor_should_run() is False


def test_ensure_dev_keys_idempotent(monkeypatch, tmp_path):
    """Same keys returned across two calls."""
    monkeypatch.delenv('LIVEKIT_API_KEY', raising=False)
    monkeypatch.delenv('LIVEKIT_API_SECRET', raising=False)
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    from integrations.social import livekit_supervisor
    a = livekit_supervisor.ensure_dev_keys()
    b = livekit_supervisor.ensure_dev_keys()
    assert a['api_key'] == b['api_key']
    assert a['api_secret'] == b['api_secret']
    assert a['api_key'].startswith('API')
    assert len(a['api_secret']) >= 32


def test_ensure_dev_keys_env_override_wins(monkeypatch, tmp_path):
    """Env-var keys take priority — no dev_keys.json written."""
    monkeypatch.setenv('LIVEKIT_API_KEY', 'OPERATOR_KEY')
    monkeypatch.setenv('LIVEKIT_API_SECRET', 'OPERATOR_SECRET')
    monkeypatch.setenv('HEVOLVE_HOME', str(tmp_path))
    from integrations.social import livekit_supervisor
    keys = livekit_supervisor.ensure_dev_keys()
    assert keys['api_key'] == 'OPERATOR_KEY'
    assert keys['api_secret'] == 'OPERATOR_SECRET'
    # No file written — env path bypasses persistence.
    assert not (tmp_path / 'livekit' / 'dev_keys.json').exists()
