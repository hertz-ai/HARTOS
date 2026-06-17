"""Phase 7d — closure pytest (#219).

Closure smoke for every existing api_calls.py endpoint + xfail-marked
documentation of the gaps identified by the scout pass.  The intent is
NOT to re-cover the deep behavior already locked by test_phase7d_calls.py
— that file is the canonical, exhaustive Phase 7d test surface.  This
file is the lightweight, parametrized "every endpoint responds, body is
well-formed JSON, no 500s" smoke + a single living record of the gaps
between the originally-planned shapes (CallSession/CallParticipant/
AgentJoinGrant ORM classes, top-level LiveKitService.create_room, the
three test file names) and what actually shipped.

Fixture pattern mirrors test_phase7d_calls.py (Flask test_client + an
in-memory DB engine).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Fixtures (parity with test_phase7d_calls.py) ──────────────────────

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
    from integrations.social.api_calls import calls_bp
    app = Flask(__name__)
    app.register_blueprint(api.social_bp)
    app.register_blueprint(calls_bp)
    yield app.test_client(), fresh_db[0]


def _seed_user(db):
    from integrations.social.models import User
    u = User(id=str(uuid.uuid4()),
             username=f'u_{uuid.uuid4().hex[:6]}',
             display_name='U',
             email=f'u_{uuid.uuid4().hex[:6]}@x.test',
             password_hash='x:y',
             user_type='human')
    db.add(u)
    db.commit()
    return u


def _seed_agent(db, owner_id):
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


def _seed_community(db, owner_id):
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
    db.commit()
    return com


def _bearer(user):
    from integrations.social import auth
    return f'Bearer {auth.generate_jwt(user.id, user.username, "flat")}'


def _existing_call(db, owner):
    """Seed a community + active call owned by ``owner`` and return the
    call session dict.  Many endpoints need an existing call_id."""
    com = _seed_community(db, owner_id=owner.id)
    from integrations.social.call_service import CallService
    return CallService.create(db, 'community', com.id, owner.id,
                              kind='voice'), com


# ── Endpoint smoke: parametrized over every shipping endpoint ──────────
#
# Per endpoint we assert:
#   1. response.status_code != 500 (no unhandled exception)
#   2. response.get_json() is a dict (well-formed JSON envelope)
#   3. envelope carries the canonical 'success' key (api_common.py)
#
# Setup callables seed any precondition rows + return:
#   (method, path, json_body or None)
# so the parametrize ids stay readable.


def _setup_start(client, db):
    user = _seed_user(db)
    com = _seed_community(db, owner_id=user.id)
    return (
        client.post,
        '/api/social/calls',
        {'parent_kind': 'community', 'parent_id': com.id, 'kind': 'voice'},
        _bearer(user),
    )


def _setup_get(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.get,
        f'/api/social/calls/{sess["id"]}',
        None,
        _bearer(user),
    )


def _setup_token(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.post,
        f'/api/social/calls/{sess["id"]}/token',
        {},
        _bearer(user),
    )


def _setup_join(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.post,
        f'/api/social/calls/{sess["id"]}/join',
        {'device_kind': 'mobile'},
        _bearer(user),
    )


def _setup_leave(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.post,
        f'/api/social/calls/{sess["id"]}/leave',
        {},
        _bearer(user),
    )


def _setup_end(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.post,
        f'/api/social/calls/{sess["id"]}/end',
        {},
        _bearer(user),
    )


def _setup_participants(client, db):
    user = _seed_user(db)
    sess, _ = _existing_call(db, user)
    return (
        client.get,
        f'/api/social/calls/{sess["id"]}/participants',
        None,
        _bearer(user),
    )


def _setup_add_agent(client, db):
    user = _seed_user(db)
    agent = _seed_agent(db, owner_id=user.id)
    sess, com = _existing_call(db, user)
    # Grant + community-membership so attach can pass — the smoke test
    # is about endpoint reachability, not the guard path.
    from sqlalchemy import text
    db.execute(text(
        "INSERT INTO memberships "
        "(id, parent_kind, parent_id, member_id, agent_kind, role) "
        "VALUES (:id, 'community', :pid, :mid, 'agent', 'member')"),
        {'id': str(uuid.uuid4()), 'pid': com.id, 'mid': agent.id})
    db.commit()
    from integrations.social.call_service import CallService
    CallService.grant_agent(
        db, agent.id, user.id, 'community', com.id,
        scope={'can_voice': True})
    return (
        client.post,
        f'/api/social/calls/{sess["id"]}/agents',
        {'agent_id': agent.id},
        _bearer(user),
    )


def _setup_create_grant(client, db):
    user = _seed_user(db)
    agent = _seed_agent(db, owner_id=user.id)
    com = _seed_community(db, owner_id=user.id)
    return (
        client.post,
        '/api/social/agent-grants',
        {'agent_id': agent.id, 'parent_kind': 'community',
         'parent_id': com.id, 'scope': {'can_voice': True}},
        _bearer(user),
    )


def _setup_revoke_grant(client, db):
    user = _seed_user(db)
    agent = _seed_agent(db, owner_id=user.id)
    com = _seed_community(db, owner_id=user.id)
    from integrations.social.call_service import CallService
    grant = CallService.grant_agent(
        db, agent.id, user.id, 'community', com.id,
        scope={'can_voice': True})
    return (
        client.delete,
        f'/api/social/agent-grants/{grant["id"]}',
        None,
        _bearer(user),
    )


ENDPOINT_SETUPS = [
    ('POST_/calls', _setup_start),
    ('GET_/calls/<id>', _setup_get),
    ('POST_/calls/<id>/token', _setup_token),
    ('POST_/calls/<id>/join', _setup_join),
    ('POST_/calls/<id>/leave', _setup_leave),
    ('POST_/calls/<id>/end', _setup_end),
    ('GET_/calls/<id>/participants', _setup_participants),
    ('POST_/calls/<id>/agents', _setup_add_agent),
    ('POST_/agent-grants', _setup_create_grant),
    ('DELETE_/agent-grants/<id>', _setup_revoke_grant),
]


@pytest.mark.parametrize(
    'endpoint_id,setup',
    ENDPOINT_SETUPS,
    ids=[e[0] for e in ENDPOINT_SETUPS],
)
def test_endpoint_no_500_and_well_formed_json(app_client, endpoint_id, setup):
    """Each api_calls.py endpoint responds without 500 and returns a
    well-formed JSON envelope carrying the canonical 'success' key."""
    client, db = app_client
    method, path, body, authz = setup(client, db)
    if body is None:
        resp = method(path, headers={'Authorization': authz})
    else:
        resp = method(path, json=body,
                      headers={'Authorization': authz})
    assert resp.status_code != 500, (
        f"{endpoint_id} returned 500 (unhandled exception): "
        f"{resp.get_data(as_text=True)[:300]}")
    payload = resp.get_json()
    assert isinstance(payload, dict), (
        f"{endpoint_id} returned non-JSON body: "
        f"{resp.get_data(as_text=True)[:300]}")
    assert 'success' in payload, (
        f"{endpoint_id} response missing canonical 'success' envelope key: "
        f"{payload}")


# ── LiveKitService.create_room + AgentVoiceBridge.attach_agent ─────────

def test_livekit_service_create_room_returns_row():
    """Scout flagged LiveKitService.create_room as missing.  The class
    has no top-level create_room — room creation is implicit/lazy via
    issue_token (which returns a dict that documents the mode +
    metadata the caller needs to either connect to the room or fall
    back to mesh).  Treat issue_token as the room-creation surrogate:
    it must return a non-empty dict (the "row") with a 'mode' key.

    When create_room lands as a real top-level method, replace the body
    with `LiveKitService.create_room(...)` and assert the same shape."""
    from integrations.social.livekit_service import LiveKitService
    has_create_room = hasattr(LiveKitService, 'create_room')
    if has_create_room:
        row = LiveKitService.create_room('call-closure')  # pragma: no cover
        assert isinstance(row, dict)
        assert 'mode' in row or 'room' in row
    else:
        # issue_token is the surrogate today — call_id acts as room name.
        row = LiveKitService.issue_token('call-closure', 'user-closure')
        assert isinstance(row, dict)
        assert row.get('call_id') == 'call-closure' or 'mode' in row
        assert 'mode' in row


def test_agent_voice_bridge_attach_agent_exists_and_idempotent():
    """AgentVoiceBridge.attach_agent should spin a worker for
    (call, agent) and be idempotent on re-attach.  Smoke only — the
    deep coverage lives in
    tests/unit/test_agent_voice_bridge_tick.py + the integration test
    in test_phase7d_calls.py."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    assert hasattr(AgentVoiceBridge, 'attach_agent')
    try:
        r1 = AgentVoiceBridge.attach_agent(
            db=None, call_id='call-closure-1',
            agent_id='agent-closure-1', owner_id='owner-closure-1',
            scope={'can_voice': True})
        assert isinstance(r1, dict)
        assert r1.get('call_id') == 'call-closure-1'
        assert r1.get('agent_id') == 'agent-closure-1'
        # Idempotent: same key returns the same worker shape.
        r2 = AgentVoiceBridge.attach_agent(
            db=None, call_id='call-closure-1',
            agent_id='agent-closure-1', owner_id='owner-closure-1',
            scope={'can_voice': True})
        assert r2.get('call_id') == r1.get('call_id')
        assert r2.get('agent_id') == r1.get('agent_id')
    finally:
        AgentVoiceBridge.shutdown_all()


# ── xfail-marked gap documentation (scout missingItems) ────────────────

@pytest.mark.xfail(reason=(
    "phase7d gap: No SQLAlchemy class CallSession — call_sessions exists "
    "only as a raw SQL table created in migration v49 (NOT v29). HARTOS "
    "v29 is the ProvisionedNode migration; calls land at v49 "
    "(integrations/social/migrations.py:1515-1620). If CallSession lands "
    "as a declarative model, importing it here should succeed."),
    strict=True)
def test_gap_call_session_model_exists():
    from integrations.social.models import CallSession  # noqa: F401
    assert CallSession is not None


@pytest.mark.xfail(reason=(
    "phase7d gap: No SQLAlchemy class CallParticipant — call_participants "
    "exists only as a raw SQL table from migration v49."),
    strict=True)
def test_gap_call_participant_model_exists():
    from integrations.social.models import CallParticipant  # noqa: F401
    assert CallParticipant is not None


@pytest.mark.xfail(reason=(
    "phase7d gap: No SQLAlchemy class AgentJoinGrant — agent_join_grants "
    "exists only as a raw SQL table from migration v49."),
    strict=True)
def test_gap_agent_join_grant_model_exists():
    from integrations.social.models import AgentJoinGrant  # noqa: F401
    assert AgentJoinGrant is not None


@pytest.mark.xfail(reason=(
    "phase7d gap: LiveKitService class has no top-level create_room "
    "method — room creation is implicit/lazy via issue_token. Methods "
    "present: issue_token, end_room, start_recording, stop_recording, "
    "_api_base_url, _resolved_config, _has_livekit_config."),
    strict=True)
def test_gap_livekit_service_has_create_room():
    from integrations.social.livekit_service import LiveKitService
    assert hasattr(LiveKitService, 'create_room')


@pytest.mark.xfail(reason=(
    "phase7d gap: tests/test_calls.py does not exist — actual file is "
    "tests/test_phase7d_calls.py (covers migration v49, CallService "
    "idempotence, membership gate, participant lifecycle, end gating, "
    "AgentJoinGrant, LiveKitService p2p_mesh default, calls_v1 flag "
    "503)."),
    strict=True)
def test_gap_test_calls_file_exists():
    assert os.path.exists(
        os.path.join(ROOT, 'tests', 'test_calls.py'))


@pytest.mark.xfail(reason=(
    "phase7d gap: tests/test_agent_voice_bridge.py does not exist — "
    "actual files are tests/unit/test_agent_voice_bridge_subscriber.py + "
    "tests/unit/test_agent_voice_bridge_tick.py."),
    strict=True)
def test_gap_test_agent_voice_bridge_file_exists():
    assert os.path.exists(
        os.path.join(ROOT, 'tests', 'test_agent_voice_bridge.py'))


@pytest.mark.xfail(reason=(
    "phase7d gap: tests/test_livekit_token.py does not exist — coverage "
    "is split across tests/unit/test_livekit_egress.py, "
    "test_livekit_audio_publisher.py, test_livekit_transcript_subscriber"
    ".py and the issue_token branch inside test_phase7d_calls.py."),
    strict=True)
def test_gap_test_livekit_token_file_exists():
    assert os.path.exists(
        os.path.join(ROOT, 'tests', 'test_livekit_token.py'))
