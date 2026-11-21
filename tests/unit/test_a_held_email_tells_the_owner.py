"""A held email tells the owner it is waiting.

Every goal email that is not verified copy is held for the owner's approval
(test_every_goal_email_is_verified_before_it_leaves.py).  Until 2026-11-21
holding it only wrote a log line, so the email waited on an owner nobody had
told.  Now:
  * the first email a goal holds tells the goal's person through the
    notification a goal that needs help already sends
    (GoalManager._tell_its_person), once, not once per draft;
  * when the owner has cleared the goal's held email, the next one tells
    them again;
  * an email of no goal (held on a node that is not flat) tells the node's
    owner;
  * the notice never carries the subject, the body or the address;
  * a telling that fails leaves the email held, and the held list reads
    exactly as it did.

The real tools, prospect store, held drafts file, GoalManager,
NotificationService and social DB (the harness of
test_each_goal_sends_from_its_own_mailbox.py); the mail servers are fakes.
"""
import json
import logging

import pytest

from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401
    HEVOLVE, MCGROCE, OWNER, _connect, _in_turn, _legacy_mailbox, _mailbox,
    _prospect, _tools, channels, mail, mailer, node)

ANA = 'ana@clinic.example'
BEN = 'ben@school.example'
BODY = '<p>Hello Ana,</p><p>would a short call next week help?</p>'
NODE_OWNER = 'node-owner-h2'


@pytest.fixture
def people():
    """Accounts on this node, and every notification written for them
    removed before and after."""
    from integrations.social.models import Notification, User, db_session
    made = []

    def clear():
        with db_session() as db:
            db.query(Notification).filter(
                Notification.user_id.in_([OWNER, NODE_OWNER])).delete(
                    synchronize_session=False)

    def add(uid):
        with db_session() as db:
            if db.query(User).filter_by(id=uid).first() is None:
                db.add(User(id=uid, username=f'user_{uid}', display_name=uid,
                            user_type='human', api_token=f'tok-{uid}'))
                made.append(uid)
        return uid
    clear()
    yield add
    clear()
    with db_session() as db:
        for uid in made:
            db.query(User).filter_by(id=uid).delete()


def _told(user_id):
    """(type, target_type, target_id, message) of each notification
    ``user_id`` has."""
    from integrations.social.models import Notification, db_session
    with db_session(commit=False) as db:
        rows = db.query(Notification).filter_by(user_id=user_id).all()
        return [(r.type, r.target_type, r.target_id, r.message) for r in rows]


def _write(tools, pid, goal='g-speech', subject='a question', body=BODY):
    return json.loads(_in_turn(goal, tools['send_outreach_email'], pid, subject, body))


def _speech(node, monkeypatch):
    _connect(HEVOLVE)
    _mailbox(node.channels, HEVOLVE)
    return _tools(monkeypatch, node.store)


def test_the_first_held_email_of_a_goal_tells_its_person(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')

    out = _write(tools, pid)

    assert out['held'] is True and out['sent'] is False
    told = _told(OWNER)
    assert len(told) == 1, told
    ((kind, target_type, target_id, message),) = told
    assert (kind, target_type, target_id) == ('goal_needs_help', 'goal', 'g-speech')
    assert 'held for your approval' in message
    assert 'approve' in message and 'reject' in message
    # Never the words or the address of the email.
    for private in ('a question', 'Hello Ana', 'short call', ANA, 'Clinic'):
        assert private not in message


def test_the_owner_is_told_once_not_once_per_draft(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')

    first = _write(tools, ana)
    again = _write(tools, ana)                    # the same email asked twice
    second = _write(tools, ben, subject='another question')

    assert first['held'] and second['held']
    assert again['draft_id'] == first['draft_id']
    assert len(node.store.list_held_drafts(goal_id='g-speech')) == 2
    assert len(_told(OWNER)) == 1


def test_the_owner_is_told_again_once_the_held_email_is_cleared(
        node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')
    draft_id = _write(tools, ana)['draft_id']
    assert node.store.reject_held_draft(draft_id, 'too pushy', rejected_by=OWNER)[
        'success'] is True
    assert node.store.list_held_drafts(goal_id='g-speech') == []

    assert _write(tools, ben, subject='another question')['held'] is True

    assert len(_told(OWNER)) == 2


def test_each_goal_tells_for_its_own_email(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    _connect(MCGROCE)
    _mailbox(node.channels, MCGROCE)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'Studio', BEN, 'g-lashes')

    assert _write(tools, ana)['held'] is True
    assert _write(tools, ben, goal='g-lashes')['held'] is True

    assert sorted(t[2] for t in _told(OWNER)) == ['g-lashes', 'g-speech']


def test_an_email_of_no_goal_tells_the_nodes_owner(node, monkeypatch, people):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', NODE_OWNER)
    people(NODE_OWNER)
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    one = _prospect(tools, 'Shop', 'mo@shop.example')
    two = _prospect(tools, 'Cafe', 'li@cafe.example')

    out = json.loads(tools['send_outreach_email'](one, 'hello', '<p>hi there</p>'))
    more = json.loads(tools['send_outreach_email'](two, 'hello', '<p>hi there</p>'))

    assert out['held'] is True and more['held'] is True
    told = _told(NODE_OWNER)
    assert len(told) == 1, told
    ((kind, target_type, target_id, message),) = told
    assert (kind, target_type, target_id) == (
        node.store.HELD_NOTICE_TYPE, 'outreach', None)
    assert 'held for your approval' in message
    assert 'mo@shop.example' not in message and 'hi there' not in message


def test_an_email_of_no_goal_with_no_owner_account_is_held_and_logged(
        node, monkeypatch, people, caplog):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', NODE_OWNER)   # no account here
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    pid = _prospect(tools, 'Shop', 'mo@shop.example')

    with caplog.at_level(logging.WARNING):
        out = json.loads(tools['send_outreach_email'](pid, 'hello', '<p>hi there</p>'))

    assert out['held'] is True
    assert _told(NODE_OWNER) == []
    assert any('nobody was told' in r.getMessage() for r in caplog.records)
    assert 'mo@shop.example' not in caplog.text


def test_a_telling_that_fails_leaves_the_email_held(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    from integrations.social.services import NotificationService

    def broken(*a, **k):
        raise RuntimeError('the notification store is down')
    monkeypatch.setattr(NotificationService, 'create', staticmethod(broken))
    from integrations.agent_engine.goal_manager import GoalManager
    monkeypatch.setattr(GoalManager, '_tell_its_person', staticmethod(broken))

    out = _write(tools, pid)

    assert out['held'] is True and out['draft_id']
    assert out['note'] == node.store.HELD_NOTE
    (draft,) = node.store.list_held_drafts(goal_id='g-speech')
    assert draft['draft_id'] == out['draft_id']


def test_the_held_list_and_the_approval_read_as_before(node, monkeypatch, people):
    """The store and what the admin routes return keep their keys: telling
    the owner adds nothing to a draft."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    out = _write(tools, pid)

    (draft,) = node.store.list_held_drafts(goal_id='g-speech')
    assert set(draft) == {'draft_id', 'goal_id', 'prospect_id', 'subject',
                          'body_html', 'status', 'first_touch', 'created_at',
                          'company'}
    sent = node.store.approve_held_draft(out['draft_id'], approved_by=OWNER)
    assert sent['sent'] is True and set(sent) == {'sent', 'draft_id', 'send_result'}
    (mailed,) = node.mail.sent_from(HEVOLVE)
    assert (mailed['to'], mailed['subject'], mailed['html']) == (ANA, 'a question', BODY)
