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

Review of 2518924bc (2026-11-22): "told" was read from another draft of the
goal still being held, not from a notice that was made, and the notice went
out inside the drafts' lock, before the save.  Now:
  * a held draft carries told_at only once a notification was made (the rule
    a goal that needs help keeps); a telling that failed, an owner with no
    account yet, and drafts held before this code are told on the goal's
    next held email, once;
  * a delivered notice is never repeated while the goal still has email of
    that wait held, whichever draft the owner clears first;
  * the notice goes out after the draft is saved and outside the lock.

The real tools, prospect store, held drafts file, GoalManager,
NotificationService and social DB (the harness of
test_each_goal_sends_from_its_own_mailbox.py); the mail servers are fakes.
"""
import json
import logging
import threading
import time

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


# ── Told means a notice was made (review of 2518924bc) ──

def _stored(node):
    """The held drafts as the file keeps them, by draft id."""
    with open(node.store._held_file(), encoding='utf-8') as f:
        return json.load(f)['drafts']


def _failing_store(monkeypatch, failures):
    """NotificationService.create raises for its first ``failures`` calls,
    then works; the list it returns counts the calls."""
    from integrations.social.services import NotificationService
    real = NotificationService.create
    calls = []

    def create(*a, **k):
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError('database is locked')
        return real(*a, **k)
    monkeypatch.setattr(NotificationService, 'create', staticmethod(create))
    return calls


def test_a_telling_that_failed_is_made_on_the_next_held_email(
        node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')
    cy = _prospect(tools, 'College', 'cy@college.example', 'g-speech')
    calls = _failing_store(monkeypatch, failures=1)

    assert _write(tools, ana)['held'] is True
    assert _told(OWNER) == []
    assert all('told_at' not in d for d in _stored(node).values())

    assert _write(tools, ben, subject='another question')['held'] is True
    assert len(_told(OWNER)) == 1
    stored = _stored(node)
    assert len(stored) == 2 and all(d.get('told_at') for d in stored.values())

    assert _write(tools, cy, subject='a third question')['held'] is True
    assert len(_told(OWNER)) == 1                 # delivered: never repeated
    assert len(calls) == 2
    assert all(d.get('told_at') for d in _stored(node).values())


def test_a_telling_that_failed_is_made_when_the_same_email_is_asked_again(
        node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    _failing_store(monkeypatch, failures=1)
    first = _write(tools, ana)
    assert _told(OWNER) == []

    again = _write(tools, ana)

    assert again['draft_id'] == first['draft_id']
    assert len(_told(OWNER)) == 1
    _write(tools, ana)
    assert len(_told(OWNER)) == 1
    assert len(_stored(node)) == 1


def test_a_telling_that_failed_is_made_when_another_first_email_is_refused(
        node, monkeypatch, people):
    """The goal writes its one prospect a different first email: refused,
    the first is still held, and the owner nobody told is told."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    _failing_store(monkeypatch, failures=1)
    first = _write(tools, ana)
    assert _told(OWNER) == []

    other = _write(tools, ana, subject='a better question')

    assert other['sent'] is False and not other.get('held')
    assert list(_stored(node)) == [first['draft_id']]
    assert len(_told(OWNER)) == 1


def test_email_held_before_this_code_is_told_once(node, monkeypatch, people):
    """A draft the store already held with no told_at (every draft held
    until 2026-11-22): the goal's next held email tells its owner, once."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')
    with node.store._held_drafts() as drafts:
        drafts['olddraft0001'] = {
            'draft_id': 'olddraft0001', 'goal_id': 'g-speech',
            'prospect_id': 'gone', 'subject': 'old', 'body_html': '<p>old</p>',
            'status': 'held', 'first_touch': True,
            'created_at': '2026-11-20T00:00:00'}
        drafts['oldreject001'] = dict(
            drafts['olddraft0001'], draft_id='oldreject001', subject='older',
            status='rejected', reason='no')
    assert _told(OWNER) == []

    assert _write(tools, ana)['held'] is True
    assert len(_told(OWNER)) == 1
    stored = _stored(node)
    assert len(stored) == 3
    # Only what is held is marked: a draft the owner already dealt with is
    # left as it was.
    assert 'told_at' not in stored.pop('oldreject001')
    assert all(d.get('told_at') for d in stored.values())

    assert _write(tools, ben, subject='another question')['held'] is True
    assert len(_told(OWNER)) == 1


def test_an_owner_with_no_account_yet_is_told_once_they_have_one(
        node, monkeypatch, people):
    """Nobody to tell is not a telling: nothing is marked, so the owner is
    told by the next held email after their account exists."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', NODE_OWNER)   # no account yet
    _legacy_mailbox(node, monkeypatch)
    tools = _tools(monkeypatch, node.store)
    one = _prospect(tools, 'Shop', 'mo@shop.example')
    two = _prospect(tools, 'Cafe', 'li@cafe.example')
    three = _prospect(tools, 'Bar', 'jo@bar.example')
    assert json.loads(tools['send_outreach_email'](
        one, 'hello', '<p>hi there</p>'))['held'] is True
    assert all('told_at' not in d for d in _stored(node).values())

    people(NODE_OWNER)
    assert json.loads(tools['send_outreach_email'](
        two, 'hello', '<p>hi there</p>'))['held'] is True
    assert json.loads(tools['send_outreach_email'](
        three, 'hello', '<p>hi there</p>'))['held'] is True

    told = _told(NODE_OWNER)
    assert len(told) == 1, told
    assert told[0][0] == node.store.HELD_NOTICE_TYPE
    assert all(d.get('told_at') for d in _stored(node).values())


def test_clearing_the_older_email_first_does_not_tell_the_owner_again(
        node, monkeypatch, people):
    """One wait, told once: the owner approves the email they were told
    about while a later one is still held, and the next held email is the
    same wait."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')
    cy = _prospect(tools, 'College', 'cy@college.example', 'g-speech')
    first = _write(tools, ana)
    second = _write(tools, ben, subject='another question')
    stored = _stored(node)
    assert stored[first['draft_id']].get('told_at')
    assert stored[second['draft_id']].get('told_at') ==         stored[first['draft_id']]['told_at']
    assert node.store.approve_held_draft(first['draft_id'], approved_by=OWNER)[
        'sent'] is True

    assert _write(tools, cy, subject='a third question')['held'] is True

    assert len(_told(OWNER)) == 1
    # Cleared altogether, the next email is a new wait and tells again.
    for draft in node.store.list_held_drafts(goal_id='g-speech'):
        node.store.reject_held_draft(draft['draft_id'], 'no', rejected_by=OWNER)
    dee = _prospect(tools, 'Nursery', 'dee@nursery.example', 'g-speech')
    assert _write(tools, dee, subject='a fourth question')['held'] is True
    assert len(_told(OWNER)) == 2


def test_the_owner_is_told_after_the_save_and_outside_the_drafts_lock(
        node, monkeypatch, people):
    """While the notification is being made another thread reads the held
    list without waiting, and the draft is already in it."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    from integrations.agent_engine.goal_manager import GoalManager
    real = GoalManager._tell_its_person
    seen = {}

    def tell(db, goal, reason):
        def read():
            seen['held'] = [d['draft_id'] for d in
                            node.store.list_held_drafts(goal_id='g-speech')]
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(3)
        seen['read_while_telling'] = not reader.is_alive()
        return real(db, goal, reason)
    monkeypatch.setattr(GoalManager, '_tell_its_person', staticmethod(tell))

    out = _write(tools, pid)

    assert out['held'] is True
    assert seen['read_while_telling'] is True
    assert seen['held'] == [out['draft_id']]
    assert len(_told(OWNER)) == 1


def test_an_email_that_could_not_be_saved_tells_nobody(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    real = node.store.os.replace

    def full(src, dst):
        if str(dst).endswith(node.store._HELD_FILE_NAME):
            raise OSError(28, 'No space left on device')
        return real(src, dst)
    monkeypatch.setattr(node.store.os, 'replace', full)

    with pytest.raises(OSError):
        node.store._hold_email('g-speech', pid, 'a question', BODY)

    monkeypatch.setattr(node.store.os, 'replace', real)
    assert node.store.list_held_drafts(goal_id='g-speech') == []
    assert _told(OWNER) == []


def test_two_emails_held_at_once_tell_the_owner_once(node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pids = [_prospect(tools, f'Clinic {i}', f'p{i}@clinic.example', 'g-speech')
            for i in range(3)]
    from integrations.agent_engine.goal_manager import GoalManager
    real = GoalManager._tell_its_person

    def slow(db, goal, reason):
        time.sleep(0.3)
        return real(db, goal, reason)
    monkeypatch.setattr(GoalManager, '_tell_its_person', staticmethod(slow))
    start = threading.Barrier(len(pids))
    results = []

    def hold(pid):
        start.wait()
        results.append(node.store._hold_email('g-speech', pid, f'q {pid}', BODY))
    threads = [threading.Thread(target=hold, args=(pid,)) for pid in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert [r.get('held') for r in results] == [True, True, True]
    assert len(_told(OWNER)) == 1
    stored = _stored(node)
    assert len(stored) == 3 and all(d.get('told_at') for d in stored.values())


def test_a_mark_that_could_not_be_saved_leaves_the_email_held(
        node, monkeypatch, people, caplog):
    """The notice was made and writing told_at fails: the email is held and
    the tool answers as for any held email; the failure is logged."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    pid = _prospect(tools, 'Clinic', ANA, 'g-speech')
    real = node.store.os.replace
    saves = []

    def second_save_fails(src, dst):
        if str(dst).endswith(node.store._HELD_FILE_NAME):
            saves.append(1)
            if len(saves) == 2:
                raise OSError(28, 'No space left on device')
        return real(src, dst)
    monkeypatch.setattr(node.store.os, 'replace', second_save_fails)

    with caplog.at_level(logging.WARNING):
        out = _write(tools, pid)

    assert out['held'] is True and out['note'] == node.store.HELD_NOTE
    assert len(_told(OWNER)) == 1
    assert any('could not record' in r.getMessage() for r in caplog.records)
    assert ANA not in caplog.text


def test_told_at_stays_in_the_store_and_out_of_every_list(
        node, monkeypatch, people):
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'School', BEN, 'g-speech')
    sent = _write(tools, ana)['draft_id']
    turned_down = _write(tools, ben, subject='another question')['draft_id']
    assert all(d.get('told_at') for d in _stored(node).values())
    node.store.approve_held_draft(sent, approved_by=OWNER)
    node.store.reject_held_draft(turned_down, 'no', rejected_by=OWNER)

    for status in ('held', 'sent', 'rejected'):
        for row in node.store.list_held_drafts(status=status):
            assert 'told_at' not in row
    assert [r['draft_id'] for r in node.store.list_held_drafts(status='sent')] == [sent]
    assert [r['draft_id'] for r in
            node.store.list_held_drafts(status='rejected')] == [turned_down]


def test_one_goals_telling_does_not_stand_for_another_goals(
        node, monkeypatch, people):
    """Two goals each hold email nobody was told about: telling the owner
    about one leaves the other still to tell."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    _connect(MCGROCE)
    _mailbox(node.channels, MCGROCE)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    ben = _prospect(tools, 'Studio', BEN, 'g-lashes')
    _failing_store(monkeypatch, failures=2)
    assert _write(tools, ana)['held'] is True
    assert _write(tools, ben, goal='g-lashes')['held'] is True
    assert _told(OWNER) == []

    _write(tools, ana)
    assert [t[2] for t in _told(OWNER)] == ['g-speech']
    stored = _stored(node)
    assert [bool(d.get('told_at')) for d in stored.values()
            if d['goal_id'] == 'g-lashes'] == [False]

    _write(tools, ben, goal='g-lashes')
    assert sorted(t[2] for t in _told(OWNER)) == ['g-lashes', 'g-speech']


def test_a_goal_with_nothing_held_tells_nobody(node, monkeypatch, people):
    """The goal's one email is being sent (approved, not held any more) when
    its agent writes that prospect another first email: refused, and with
    nothing held there is nothing to tell the owner about."""
    people(OWNER)
    tools = _speech(node, monkeypatch)
    ana = _prospect(tools, 'Clinic', ANA, 'g-speech')
    draft_id = _write(tools, ana)['draft_id']
    with node.store._held_drafts() as drafts:
        drafts[draft_id]['status'] = 'sending'
        drafts[draft_id].pop('told_at', None)
    assert len(_told(OWNER)) == 1

    other = _write(tools, ana, subject='a better question')

    assert other['sent'] is False and not other.get('held')
    assert len(_told(OWNER)) == 1
    assert 'told_at' not in _stored(node)[draft_id]
