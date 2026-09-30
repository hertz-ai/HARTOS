"""POST /api/social/dashboard/agents/<goal>/inject: only the goal's owner steers it.

Review of de3f89364 (ed08f5fc, 2026-09-27, CRITICAL): the desktop owner's copy
of a guest's computer-use step carried the guest's goal id, Nunba treated it
as the live run, and the owner's typed chat was POSTed to this route for the
GUEST's goal.  The route had no ownership check at all, and /api/social/ is
exempt from the API gate, so any caller on the network could write into any
running agent's GroupChat.

The rule these pin (dashboard_service.may_steer):
  * a caller steers a goal it owns (core.event_attribution.goal_owner_user_id);
  * an admin (integrations.social.auth.is_admin_user) steers any goal;
  * a goal with no human owner (the flywheel's seeded goals) is the machine's:
    this machine's own callers steer it (the MCP co-pilot does), a remote
    non-admin does not;
  * a loopback caller IS this desktop's owner (HEVOLVE_OWNER_USER_ID, the
    identity every other owner surface uses); a remote caller is whoever its
    token says (require_local_or_auth), and no token is a 401.

Behavioural: the real blueprint, the real inject_instruction, a real SQLite
agent_goals table and the real GroupChat registry.  Patched boundaries only:
get_db (to the in-memory session), the token store (_get_user_from_token),
and the audit log's disk.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from hartos.lifecycle_hooks import register_groupchat_for_session  # noqa: E402
from integrations.social.api_dashboard import dashboard_bp  # noqa: E402
from integrations.social.models import AgentGoal, Base  # noqa: E402

REMOTE = {'REMOTE_ADDR': '203.0.113.7'}


class _GC:
    def __init__(self):
        self.messages = []


@pytest.fixture(scope='module')
def engine():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def client(session_factory, monkeypatch):
    for var in ('HEVOLVE_OWNER_USER_ID', 'TRUSTED_PROXY', 'NUNBA_CI',
                'HEVOLVE_TRUST_KONG', 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(var, raising=False)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(dashboard_bp)
    with patch('integrations.social.models.get_db',
               side_effect=lambda: session_factory()), \
         patch('security.immutable_audit_log.get_audit_log'):
        yield app.test_client()


def _goal(session_factory, owner_id=None, created_by=None):
    """A running goal with a live GroupChat, keyed the way the daemon keys it."""
    db = session_factory()
    gid = uuid.uuid4().hex
    pid = str(uuid.uuid4().int % 10**9)
    db.add(AgentGoal(id=gid, owner_id=owner_id, created_by=created_by,
                     goal_type='coding', title='t', prompt_id=pid))
    db.commit()
    db.close()
    gc = _GC()
    register_groupchat_for_session(f'{owner_id or "system"}_{pid}', gc)
    return gid, gc


def _as_token_user(user_id, is_admin=False, role='flat'):
    user = SimpleNamespace(id=user_id, is_admin=is_admin, role=role,
                           is_banned=False)
    return patch('integrations.social.auth._get_user_from_token',
                 return_value=(user, MagicMock()))


def _inject(client, gid, text='steer this', headers=None, environ=None):
    return client.post(f'/api/social/dashboard/agents/{gid}/inject',
                       json={'instruction': text, 'actor_id': 'nunba-chat'},
                       headers=headers or {}, environ_base=environ or {})


# ── the defect: the desktop owner's chat into a guest's goal ─────────────

def test_the_desktop_owner_cannot_steer_a_guests_goal(client, session_factory,
                                                      monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    gid, gc = _goal(session_factory, owner_id='guest-9')
    resp = _inject(client, gid, 'owner typed this')
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()['success'] is False
    assert gc.messages == []


def test_the_desktop_owner_steers_their_own_goal(client, session_factory,
                                                 monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    gid, gc = _goal(session_factory, owner_id='owner-1')
    resp = _inject(client, gid, 'use the cloud model')
    assert resp.status_code == 200, resp.get_json()
    assert [m['content'] for m in gc.messages] == ['use the cloud model']


def test_this_machine_steers_a_goal_with_no_human_owner(client, session_factory,
                                                        monkeypatch):
    """The MCP co-pilot's case: a seeded flywheel goal (machine author)."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    gid, gc = _goal(session_factory, created_by='system_bootstrap')
    resp = _inject(client, gid, 'emit the recipe JSON')
    assert resp.status_code == 200, resp.get_json()
    assert len(gc.messages) == 1


def test_a_local_caller_with_no_owner_configured_cannot_steer_a_users_goal(
        client, session_factory):
    gid, gc = _goal(session_factory, owner_id='guest-9')
    assert _inject(client, gid).status_code == 403
    assert gc.messages == []


# ── remote callers: a token names the caller ─────────────────────────────

def test_a_remote_caller_without_a_token_is_refused(client, session_factory):
    gid, gc = _goal(session_factory, owner_id='guest-9')
    assert _inject(client, gid, environ=REMOTE).status_code == 401
    assert gc.messages == []


def test_a_remote_owner_with_a_token_steers_their_goal(client, session_factory):
    gid, gc = _goal(session_factory, owner_id='guest-9')
    with _as_token_user('guest-9'):
        resp = _inject(client, gid, 'hi', headers={'Authorization': 'Bearer t'},
                       environ=REMOTE)
    assert resp.status_code == 200, resp.get_json()
    assert len(gc.messages) == 1


def test_a_remote_non_owner_with_a_token_is_refused(client, session_factory):
    gid, gc = _goal(session_factory, owner_id='guest-9')
    with _as_token_user('someone-else'):
        resp = _inject(client, gid, headers={'Authorization': 'Bearer t'},
                       environ=REMOTE)
    assert resp.status_code == 403
    assert gc.messages == []


def test_a_remote_non_admin_cannot_steer_a_machine_goal(client, session_factory):
    gid, gc = _goal(session_factory, created_by='system_bootstrap')
    with _as_token_user('someone-else'):
        resp = _inject(client, gid, headers={'Authorization': 'Bearer t'},
                       environ=REMOTE)
    assert resp.status_code == 403
    assert gc.messages == []


@pytest.mark.parametrize('is_admin, role', [(True, 'flat'), (False, 'central')])
def test_an_admin_steers_any_goal(client, session_factory, is_admin, role):
    gid, gc = _goal(session_factory, owner_id='guest-9')
    with _as_token_user('ops-1', is_admin=is_admin, role=role):
        resp = _inject(client, gid, headers={'Authorization': 'Bearer t'},
                       environ=REMOTE)
    assert resp.status_code == 200, resp.get_json()
    assert len(gc.messages) == 1


def test_the_refusal_is_audited_with_the_callers_identity(client, session_factory,
                                                          monkeypatch):
    """A refused steer names who tried, so an owner can see it happened."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    gid, _ = _goal(session_factory, owner_id='guest-9')
    audit = MagicMock()
    with patch('security.immutable_audit_log.get_audit_log', return_value=audit):
        assert _inject(client, gid).status_code == 403
    events = [c.kwargs for c in audit.log_event.call_args_list]
    assert events and events[-1]['action'] == 'inject_refused'
    assert events[-1]['detail']['caller_user_id'] == 'owner-1'
    assert events[-1]['target_id'] == gid
