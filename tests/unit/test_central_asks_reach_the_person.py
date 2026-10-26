"""What a node needs from a person reaches that person, on their phone too.

Owner, 2026-10-26: "Why is central not asking me to unblock it to earn
revenue?" and "why HARTOS fanout does not reach phone via firebase is the
question".  Measured on central that day (logs back to 2 Sep): the only asks
ever filed were 8 copies of one computer-control ask; five earning goals had
paused needing a person and none told anyone, because the escalation's human
stage (GoalManager.escalate_goal) only wrote "Needs help: ..." into the goal;
and the one fan-out every ask and notification takes
(integrations.social.realtime.on_notification) had no Firebase leg, so an
app that was closed or listening elsewhere never heard of either.

Real ConsentService, NotificationService, GoalManager and in-memory social
DB; the boundary is core.fcm_sync.send_fcm_push (Google / central's relay),
recorded.  No grep tests.

    python -m pytest tests/unit/test_central_asks_reach_the_person.py -q
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, Notification, User, db_session, get_engine)


def _settle():
    """Wait for the pushes the fan-out started off the caller's thread."""
    for t in [t for t in threading.enumerate() if t.name == 'notify-push']:
        t.join(timeout=5)


@pytest.fixture
def pushes(monkeypatch):
    """Every Firebase message the node tried to send: (user, title, body, data).

    A node's own process pushes; a test process only when it sets the
    background-services switch, as these tests do."""
    Base.metadata.create_all(get_engine())
    monkeypatch.setenv('HEVOLVE_START_BACKGROUND_SERVICES', '1')
    import core.fcm_sync as fcm
    import integrations.social.realtime as rt
    sent = []

    def fake_send(user_id, title, body, data=None, **_k):
        sent.append((str(user_id), title, body, dict(data or {})))
        return True

    monkeypatch.setattr(fcm, 'send_fcm_push', fake_send)
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    pushed_ids = getattr(rt, '_PUSHED', None)   # the once-per-msg_id memory
    if pushed_ids is not None:
        pushed_ids.clear()
    yield sent
    _settle()


def _person(uid):
    with db_session() as db:
        if not db.query(User).filter_by(id=uid).first():
            db.add(User(id=uid, username=f'p{uid}', display_name=f'P {uid}',
                        user_type='human', api_token=f'tok-{uid}'))


def _goal(**kw):
    with db_session() as db:
        g = AgentGoal(goal_type='marketing', title=kw.pop('title', 'Grow the network'),
                      description='', config_json={}, status='active', **kw)
        db.add(g)
        db.flush()
        return g.id


def _escalate(goal_id, monkeypatch):
    from integrations.agent_engine.goal_manager import GoalManager
    monkeypatch.setattr(GoalManager, '_escalation_expert',
                        staticmethod(lambda goal_type=None: None))
    with db_session() as db:
        return GoalManager.escalate_goal(db, goal_id, {
            'action_id': 2, 'action': 'present top options to the customer',
            'reason': 'the verifier says it needs input from a person'})


def _notifications(uid, goal_id):
    """The notifications ``uid`` got about one goal (the DB is shared by the
    whole file, so earlier tests' goals are there too)."""
    with db_session(commit=False) as db:
        return [(n.type, n.target_id, n.message)
                for n in db.query(Notification).filter_by(
                    user_id=uid, target_id=goal_id).all()]


# ── the fan-out's Firebase leg ──────────────────────────────────────────────

def test_a_notification_with_a_push_rings_the_phone_once(pushes):
    from integrations.social.realtime import on_notification
    push = {'title': 'T', 'body': 'B', 'data': {'type': 'goal_needs_help'}}
    on_notification('u1', {'type': 'x', 'msg_id': 'm-1'}, push=push)
    on_notification('u1', {'type': 'x', 'msg_id': 'm-1'}, push=push)
    _settle()
    assert pushes == [('u1', 'T', 'B', {'type': 'goal_needs_help'})]


def test_a_notification_without_a_push_never_rings_the_phone(pushes):
    from integrations.social.realtime import on_notification
    on_notification('u1', {'type': 'x', 'msg_id': 'm-2'})
    _settle()
    assert pushes == []


def test_a_process_without_background_services_sends_no_push(pushes, monkeypatch, caplog):
    import integrations.social.realtime as rt
    from integrations.social.realtime import on_notification
    monkeypatch.delenv('HEVOLVE_START_BACKGROUND_SERVICES')   # pytest's default: off
    monkeypatch.setattr(rt, '_push_skip_said', False, raising=False)
    push = {'title': 'T', 'body': 'B'}
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        on_notification('u1', {'type': 'x', 'msg_id': 'm-off-1'}, push=push)
        on_notification('u1', {'type': 'x', 'msg_id': 'm-off-2'}, push=push)
    _settle()
    assert pushes == []
    said = [r for r in caplog.records if 'pushes are off' in r.getMessage()]
    assert len(said) == 1


def test_a_slow_push_never_holds_up_the_caller(pushes, monkeypatch):
    import core.fcm_sync as fcm
    from integrations.social.realtime import on_notification
    release = threading.Event()
    monkeypatch.setattr(fcm, 'send_fcm_push',
                        lambda *a, **k: release.wait(10) or True)
    started = time.monotonic()
    on_notification('u1', {'type': 'x', 'msg_id': 'm-3'},
                    push={'title': 'T', 'body': 'B'})
    assert time.monotonic() - started < 1.0
    release.set()


# ── a consent ask rings the phone, once ─────────────────────────────────────

def test_a_new_ask_is_pushed_once_however_often_it_is_re_sent(pushes):
    from integrations.social.consent_service import ConsentService
    _person('10202')
    with db_session() as db:
        row = ConsentService.request_consent(
            db, '10202', 'data_access', agent_id=None,
            reason='The marketing agent needs your LinkedIn page to post')
        row_id = row.id
    with db_session() as db:   # the next poll re-sends the same ask
        ConsentService.request_consent(db, '10202', 'data_access', agent_id=None)
    _settle()
    assert len(pushes) == 1
    user, title, body, data = pushes[0]
    assert user == '10202'
    assert data['type'] == 'consent_prompt'
    assert data['request_id'] == row_id
    assert data['user_id'] == '10202'
    assert data['topic_reply'] == 'com.hertzai.pupit.10202'   # where the answer goes
    assert 'LinkedIn' in body and title


def test_a_device_ask_for_a_capability_rings_the_phone_once_with_the_asks_id(
        pushes, monkeypatch):
    """DeviceRoutingService files the ask through ConsentService, which now
    rings the phone; its own push must not ring it a second time with an id
    no ask on file has, and the fleet command carries the same id."""
    from integrations.social.device_routing_service import DeviceRoutingService
    from integrations.social.fleet_command import FleetCommandService
    from integrations.social.models import DeviceBinding, UserConsent
    monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)  # central skips a screen ask
    _person('10204')
    with db_session() as db:
        db.add(DeviceBinding(id='bind-10204', user_id='10204', device_id='dev-10204',
                             device_name='Phone', platform='android',
                             form_factor='phone', is_active=True))
    commands = []
    monkeypatch.setattr(FleetCommandService, 'push_command', staticmethod(
        lambda db, node_id, cmd_type, params, issued_by='':
            commands.append((node_id, cmd_type, params)) or {'id': 'cmd-1'}))
    with db_session() as db:
        out = DeviceRoutingService.request_consent(
            db, '10204', 'enable_screen', 'agent-s',
            description='to read the error on your screen')
    _settle()
    with db_session(commit=False) as db:
        ask_id = db.query(UserConsent).filter_by(
            user_id='10204', consent_type='screen_capture').one().id
    prompts = [(u, d['request_id']) for u, _t, _b, d in pushes
               if d.get('type') == 'consent_prompt']
    assert prompts == [('10204', ask_id)]
    assert out['request_id'] == ask_id
    assert [(n, t, p['request_id']) for n, t, p in commands] == [
        ('dev-10204', 'agent_consent', ask_id)]


def test_a_grant_announcement_is_not_pushed(pushes):
    from integrations.social import consent_service as cs
    cs._emit('consent.granted', {'user_id': '10202', 'consent_type': 'data_access'},
             msg_id='consent.granted:abc')
    _settle()
    assert pushes == []


def test_what_leaves_for_the_phone_is_scrubbed(pushes):
    from integrations.social.consent_service import ConsentService
    _person('10203')
    with db_session() as db:
        ConsentService.request_consent(
            db, '10203', 'data_access', agent_id=None,
            reason='Write to alice.person@example.com about the order')
    _settle()
    assert len(pushes) == 1
    _u, title, body, data = pushes[0]
    for text in (title, body, data.get('body', ''), data.get('title', '')):
        assert 'alice.person@example.com' not in text


# ── a goal that needs a person tells that person ────────────────────────────

def test_a_machine_goal_that_needs_help_tells_the_nodes_owner(pushes, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Grow the network')
    result = _escalate(gid, monkeypatch)
    _settle()
    assert result.get('stage') == 'human'
    with db_session(commit=False) as db:
        goal = db.query(AgentGoal).filter_by(id=gid).first()
        assert goal.status == 'paused'
        assert goal.config_json['pause_reason'].startswith('Needs help')
    notes = _notifications('10202', gid)
    assert [(t, tid) for t, tid, _m in notes] == [('goal_needs_help', gid)]
    assert 'needs input from a person' in notes[0][2]
    assert len(pushes) == 1
    user, _title, body, data = pushes[0]
    assert user == '10202'
    assert data == {'type': 'goal_needs_help', 'goal_id': gid}
    assert 'Grow the network' in body


def test_a_persons_own_goal_tells_that_person_not_the_node_owner(pushes, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    _person('u-alice')
    gid = _goal(owner_id='u-alice', created_by='u-alice')
    _escalate(gid, monkeypatch)
    _settle()
    assert [t for t, _g, _m in _notifications('u-alice', gid)] == ['goal_needs_help']
    assert _notifications('10202', gid) == []
    assert [p[0] for p in pushes] == ['u-alice']


def test_the_same_stuck_action_asked_again_while_it_waits_tells_nobody_again(
        pushes, monkeypatch):
    """A hive worker re-runs a paused goal's task each time its claim
    expires (#194, ~1,470 model calls a day on central); each re-run asks
    for help with the same action.  The owner hears about it once."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Find buyers')
    for _ in range(3):
        assert _escalate(gid, monkeypatch).get('stage') == 'human'
    _settle()
    assert [t for t, _g, _m in _notifications('10202', gid)] == ['goal_needs_help']
    assert [p[3].get('goal_id') for p in pushes] == [gid]


def test_with_nobody_to_ask_the_goal_still_pauses_and_says_so(pushes, monkeypatch, caplog):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    gid = _goal(created_by='system_bootstrap')
    with caplog.at_level(logging.WARNING):
        result = _escalate(gid, monkeypatch)
    _settle()
    assert result.get('stage') == 'human'
    with db_session(commit=False) as db:
        assert db.query(AgentGoal).filter_by(id=gid).first().status == 'paused'
        assert db.query(Notification).filter_by(target_id=gid).count() == 0
    assert pushes == []
    assert any(gid in r.getMessage() and 'nobody' in r.getMessage()
               for r in caplog.records)


def test_an_owner_with_no_account_here_leaves_the_pause_intact(pushes, monkeypatch, caplog):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'no-such-account')
    gid = _goal(created_by='system_bootstrap')
    with caplog.at_level(logging.WARNING):
        result = _escalate(gid, monkeypatch)
    _settle()
    assert result.get('stage') == 'human'
    with db_session(commit=False) as db:
        assert db.query(AgentGoal).filter_by(id=gid).first().status == 'paused'
    assert _notifications('no-such-account', gid) == []
    assert pushes == []
    assert any('no-such-account' in r.getMessage() for r in caplog.records)
