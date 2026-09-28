"""Every steering verb on the dashboard asks the ONE rule, may_steer.

Follow-up to c3651a483 (coordinator, 2026-09-27, owner-delegated defaults):

1. A goal run by this node's own daemon identity (hevolve_system_agent,
   user_type 'agent' with no human owner) is the machine's, like a goal no
   human owns: the local owner steers it.  Resolved through the canonical
   "which person is behind this id" rule, UserService.person_to_notify, so an
   agent OWNED by a person counts as that person, and another human's goal
   (or their agent's) stays refused.
2. pause / resume / cancel had the same hole as inject: no identity, no
   ownership check, under the gate-exempt /api/social/ prefix.  They now go
   through may_steer too.  test_every_steering_route_* enumerates the
   blueprint's POST routes on /dashboard/agents/<agent_id>/..., so a steering
   route added later without the rule fails here.

Behavioural: the real blueprint, the real dashboard_service, a real SQLite
users + agent_goals schema, the real GroupChat registry.  Patched boundaries
only: get_db (to the in-memory session), the token store, the audit log.
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
from integrations.social.models import AgentGoal, Base, User  # noqa: E402

REMOTE = {'REMOTE_ADDR': '203.0.113.7'}
PREFIX = '/api/social/dashboard/agents/<agent_id>/'


class _GC:
    def __init__(self):
        self.messages = []


@pytest.fixture(scope='module')
def engine():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def sf(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def app(sf, monkeypatch):
    for var in ('HEVOLVE_OWNER_USER_ID', 'TRUSTED_PROXY', 'NUNBA_CI',
                'HEVOLVE_TRUST_KONG', 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.register_blueprint(dashboard_bp)
    with patch('integrations.social.models.get_db', side_effect=lambda: sf()), \
         patch('security.immutable_audit_log.get_audit_log'):
        yield a


@pytest.fixture
def client(app):
    return app.test_client()


def _user(sf, user_type='human', owner_id=None, username=None):
    db = sf()
    u = User(username=username or f'u_{uuid.uuid4().hex[:10]}',
             user_type=user_type, owner_id=owner_id)
    db.add(u)
    db.commit()
    uid = str(u.id)
    db.close()
    return uid


def _goal(sf, owner_id=None, created_by=None, status='active'):
    db = sf()
    gid = uuid.uuid4().hex
    pid = str(uuid.uuid4().int % 10**9)
    db.add(AgentGoal(id=gid, owner_id=owner_id, created_by=created_by,
                     goal_type='coding', title='t', prompt_id=pid,
                     status=status))
    db.commit()
    db.close()
    gc = _GC()
    register_groupchat_for_session(f'{owner_id or "system"}_{pid}', gc)
    return gid, gc


def _status(sf, gid):
    db = sf()
    try:
        return db.query(AgentGoal).filter(AgentGoal.id == gid).first().status
    finally:
        db.close()


def _post(client, gid, verb, headers=None, environ=None):
    body = {'instruction': 'go', 'reason': 'test', 'actor_id': 'x'}
    return client.post(f'/api/social/dashboard/agents/{gid}/{verb}', json=body,
                       headers=headers or {}, environ_base=environ or {})


def _as_token_user(user_id, is_admin=False, role='flat'):
    user = SimpleNamespace(id=user_id, is_admin=is_admin, role=role,
                           is_banned=False)
    return patch('integrations.social.auth._get_user_from_token',
                 return_value=(user, MagicMock()))


def _steering_verbs(app):
    """Every POST route under /dashboard/agents/<agent_id>/, from the app."""
    verbs = sorted(r.rule[len(PREFIX):] for r in app.url_map.iter_rules()
                   if r.rule.startswith(PREFIX) and 'POST' in r.methods)
    assert verbs, 'no steering routes found -- the enumeration is broken'
    return verbs


# ── 1. system-agent goals are the local owner's ─────────────────────────

def test_the_local_owner_steers_a_system_agent_goal(client, sf):
    sys_agent = _user(sf, user_type='agent')  # no human owner
    gid, gc = _goal(sf, owner_id=sys_agent)
    assert _post(client, gid, 'inject').status_code == 200
    assert len(gc.messages) == 1
    assert _post(client, gid, 'pause').status_code == 200
    assert _status(sf, gid) == 'paused'


def test_a_remote_non_admin_cannot_steer_a_system_agent_goal(client, sf):
    sys_agent = _user(sf, user_type='agent')
    gid, gc = _goal(sf, owner_id=sys_agent)
    with _as_token_user('someone-else'):
        r = _post(client, gid, 'inject', {'Authorization': 'Bearer t'}, REMOTE)
    assert r.status_code == 403
    assert gc.messages == []


def test_an_agent_owned_by_another_person_stays_theirs(client, sf):
    guest = _user(sf)
    their_agent = _user(sf, user_type='agent', owner_id=guest)
    gid, gc = _goal(sf, owner_id=their_agent)
    assert _post(client, gid, 'inject').status_code == 403  # local owner
    assert gc.messages == []
    with _as_token_user(guest):  # the person behind the agent
        r = _post(client, gid, 'inject', {'Authorization': 'Bearer t'}, REMOTE)
    assert r.status_code == 200, r.get_json()
    assert len(gc.messages) == 1


def test_another_humans_goal_stays_refused_to_the_local_owner(client, sf):
    guest = _user(sf)
    gid, gc = _goal(sf, owner_id=guest)
    assert _post(client, gid, 'inject').status_code == 403
    assert gc.messages == []


# ── 2. one rule for every steering verb ─────────────────────────────────

def test_the_steering_verbs_are_the_four_known_ones(app):
    """If this changes, the tests below already cover the new verb."""
    assert _steering_verbs(app) == ['cancel', 'inject', 'pause', 'resume']


def test_every_steering_route_refuses_the_owner_on_another_users_goal(app, client, sf):
    guest = _user(sf)
    for verb in _steering_verbs(app):
        # resume is only legal from paused: start there so a missing check
        # would succeed rather than fail for an unrelated reason.
        gid, gc = _goal(sf, owner_id=guest,
                        status='paused' if verb == 'resume' else 'active')
        before = _status(sf, gid)
        r = _post(client, gid, verb)
        assert r.status_code == 403, (verb, r.status_code, r.get_json())
        assert _status(sf, gid) == before, verb
        assert gc.messages == [], verb


def test_every_steering_route_needs_a_token_from_another_machine(app, client, sf):
    guest = _user(sf)
    for verb in _steering_verbs(app):
        gid, _ = _goal(sf, owner_id=guest,
                       status='paused' if verb == 'resume' else 'active')
        r = _post(client, gid, verb, environ=REMOTE)
        assert r.status_code == 401, (verb, r.status_code)
        assert _status(sf, gid) in ('active', 'paused')


def test_every_steering_route_admits_the_owner_with_a_token(app, client, sf):
    guest = _user(sf)
    for verb in _steering_verbs(app):
        gid, gc = _goal(sf, owner_id=guest,
                        status='paused' if verb == 'resume' else 'active')
        with _as_token_user(guest):
            r = _post(client, gid, verb, {'Authorization': 'Bearer t'}, REMOTE)
        assert r.status_code == 200, (verb, r.get_json())


@pytest.mark.parametrize('verb, start, end', [
    ('pause', 'active', 'paused'),
    ('resume', 'paused', 'active'),
    ('cancel', 'active', 'archived'),
])
def test_the_owner_still_pauses_resumes_and_cancels_their_own(client, sf, verb,
                                                            start, end):
    gid, _ = _goal(sf, owner_id='owner-1', status=start)
    r = _post(client, gid, verb)
    assert r.status_code == 200, r.get_json()
    assert _status(sf, gid) == end


def test_a_refused_pause_is_audited_with_the_caller(client, sf):
    guest = _user(sf)
    gid, _ = _goal(sf, owner_id=guest)
    audit = MagicMock()
    with patch('security.immutable_audit_log.get_audit_log', return_value=audit):
        assert _post(client, gid, 'pause').status_code == 403
    ev = audit.log_event.call_args.kwargs
    assert ev['action'] == 'pause_refused'
    assert ev['detail']['caller_user_id'] == 'owner-1'


# ── review of c3651a483 (friction) ──────────────────────────────────────

LOCAL_TOKEN = {'Authorization': 'Bearer t'}


def test_a_local_request_with_a_token_is_the_tokens_user(app, client, sf):
    """Finding 1: HEVOLVE_OWNER_USER_ID is set at boot and goes stale when
    someone signs in afterwards; a signed-in desktop user got 403 on their
    own goal.  A valid token names the caller, locally too."""
    me = _user(sf)
    for verb in _steering_verbs(app):
        gid, _ = _goal(sf, owner_id=me,
                       status='paused' if verb == 'resume' else 'active')
        with _as_token_user(me):
            r = _post(client, gid, verb, LOCAL_TOKEN)
        assert r.status_code == 200, (verb, r.get_json())


def test_a_local_token_does_not_borrow_the_boot_owners_goals(client, sf):
    gid, gc = _goal(sf, owner_id='owner-1')
    with _as_token_user(_user(sf)):
        assert _post(client, gid, 'inject', LOCAL_TOKEN).status_code == 403
    assert gc.messages == []


def test_a_local_request_with_an_invalid_token_is_the_local_owner(client, sf):
    gid, gc = _goal(sf, owner_id='owner-1')
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(None, MagicMock())):
        assert _post(client, gid, 'inject', LOCAL_TOKEN).status_code == 200
    assert len(gc.messages) == 1


def test_an_agent_steers_its_owners_goal(client, sf):
    """Finding 2: an agent counts as its owner (UserService.person_to_notify,
    the rule thought_experiment_service and the task-assign route apply)."""
    guest = _user(sf)
    agent = _user(sf, user_type='agent', owner_id=guest)
    gid, gc = _goal(sf, owner_id=guest)
    with _as_token_user(agent):
        r = _post(client, gid, 'inject', LOCAL_TOKEN, REMOTE)
    assert r.status_code == 200, r.get_json()
    assert len(gc.messages) == 1


def test_an_agent_cannot_steer_a_goal_its_owner_does_not_own(client, sf):
    guest, other = _user(sf), _user(sf)
    agent = _user(sf, user_type='agent', owner_id=guest)
    gid, gc = _goal(sf, owner_id=other)
    with _as_token_user(agent):
        r = _post(client, gid, 'inject', LOCAL_TOKEN, REMOTE)
    assert r.status_code == 403
    assert gc.messages == []


def test_an_unknown_goal_and_someone_elses_answer_the_same(app, client, sf):
    """Finding 4: 403 for 'not yours' but 400 for 'no such goal' told a
    stranger which goal ids exist."""
    gid, _ = _goal(sf, owner_id=_user(sf))
    for verb in _steering_verbs(app):
        theirs = _post(client, gid, verb)
        missing = _post(client, uuid.uuid4().hex, verb)
        assert theirs.status_code == missing.status_code == 403, verb
        assert theirs.get_json() == missing.get_json(), verb


def test_an_agent_no_person_owns_cannot_steer_a_persons_goal(client, sf):
    """An ownerless agent/system account resolves to nobody; nobody is not
    a match for anyone's goal."""
    stray = _user(sf, user_type='agent')
    gid, gc = _goal(sf, owner_id=_user(sf))
    with _as_token_user(stray):
        r = _post(client, gid, 'inject', LOCAL_TOKEN, REMOTE)
    assert r.status_code == 403
    assert gc.messages == []
