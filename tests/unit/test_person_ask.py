"""The server asks the person through Liquid UI, as a tool.

Owner, 2026-10-27: "liquid ui is asked as a tool by server", "the mechanism
shd be same agnostic of hartos consumer, liqiuid ui shd also fanout", "same
for consents via firebase shd fanout in desktop, android, wamp etc".
Design: docs/design/PERSON_ASK_LIQUID_UI_TOOL.md.

One ask is one Notification row, created by NotificationService.create with a
push (the Firebase leg of realtime.on_notification), and one Liquid UI card,
all under one id.  The person answers on any surface; the first answer wins;
the asking agent gets it; the row is marked read, which is what drops the card
on every other surface.

Real NotificationService, realtime fan-out and in-memory social DB.  The
boundaries are recorded: core.fcm_sync.send_fcm_push (Google / central's
relay), realtime.publish_event (WAMP) and liquid_ui_service.push_agent_ui
(the card's own fan-out).  No grep tests.

    python -m pytest tests/unit/test_person_ask.py -q
"""
from __future__ import annotations

import json
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
    Base, Notification, User, db_session, get_engine)

CLIPS = {'type': 'file_request', 'title': 'Send me the clips for your reel',
         'purpose': 'reel_source', 'accept': 'video', 'max_files': 5,
         'prompt_id': '342'}


def _settle():
    for t in [t for t in threading.enumerate() if t.name == 'notify-push']:
        t.join(timeout=5)


@pytest.fixture
def legs(monkeypatch):
    """What left the node for the person: Firebase pushes, WAMP events and
    Liquid UI cards."""
    Base.metadata.create_all(get_engine())
    monkeypatch.setenv('HEVOLVE_START_BACKGROUND_SERVICES', '1')
    import core.fcm_sync as fcm
    import integrations.agent_engine.liquid_ui_service as lui
    import integrations.social.realtime as rt
    got = {'pushes': [], 'events': [], 'cards': []}

    def fake_send(user_id, title, body, data=None, **_k):
        got['pushes'].append((str(user_id), title, body, dict(data or {})))
        return True

    monkeypatch.setattr(fcm, 'send_fcm_push', fake_send)
    monkeypatch.setattr(rt, 'publish_event',
                        lambda topic, data, user_id=None: got['events'].append(
                            (topic, dict(data), user_id)))
    monkeypatch.setattr(lui, 'push_agent_ui',
                        lambda agent_id, card, user_id=None: got['cards'].append(
                            (agent_id, dict(card), user_id)) or True)
    pushed = getattr(rt, '_PUSHED', None)
    if pushed is not None:
        pushed.clear()
    for uid in ('10202', '777'):
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'p{uid}', display_name=f'P {uid}',
                            user_type='human', api_token=f'tok-{uid}'))
    yield got
    _settle()


def _row(ask_id):
    with db_session(commit=False) as db:
        n = db.query(Notification).filter_by(id=ask_id).first()
        return None if n is None else {
            'user_id': n.user_id, 'type': n.type, 'is_read': bool(n.is_read),
            'target_type': n.target_type, 'target_id': n.target_id,
            'message': n.message}


def _ask(**kw):
    from integrations.agent_engine import person_ask
    args = {'user_id': '10202', 'agent_id': '342', 'component': dict(CLIPS),
            'prompt_id': '342', 'title': 'Your Reel Studio agent needs your clips',
            'body': 'Pick the clips for your reel.'}
    args.update(kw)
    return person_ask.ask(args.pop('user_id'), args.pop('agent_id'),
                          args.pop('component'), **args)


# -- one ask, every leg, one id ------------------------------------------------

def test_an_ask_is_one_row_one_push_and_one_card_under_one_id(legs):
    out = _ask()
    _settle()
    assert out['ok'] is True and out['refused'] is None
    ask_id = out['ask_id']
    row = _row(ask_id)
    assert row == {'user_id': '10202', 'type': 'agent_ask', 'is_read': False,
                   'target_type': 'person_ask', 'target_id': '342',
                   'message': 'Your Reel Studio agent needs your clips'}
    assert len(legs['pushes']) == 1
    uid, title, _body, data = legs['pushes'][0]
    assert uid == '10202' and title == 'Your Reel Studio agent needs your clips'
    assert data['type'] == 'ui_ask' and data['request_id'] == ask_id
    assert data['topic_reply'] == 'com.hertzai.pupit.10202'
    assert data['component_type'] == 'file_request'
    assert len(legs['cards']) == 1
    agent_id, card, card_user = legs['cards'][0]
    assert (agent_id, card_user) == ('342', '10202')
    assert card['ask_id'] == ask_id and card['msg_id'] == ask_id
    assert card['type'] == 'file_request' and card['purpose'] == 'reel_source'
    # realtime.on_notification's payload carries the row's own type
    # ('agent_ask'), which overrides its 'notification' default; the row's
    # WAMP copy goes out under the ask's id, once.
    notes = [d for t, d, _u in legs['events']
             if t == 'chat.social' and d.get('msg_id') == ask_id]
    assert [(n['type'], n['id']) for n in notes] == [('agent_ask', ask_id)]


def test_the_ask_rings_the_phone_once_however_often_it_is_fanned(legs):
    from integrations.social.realtime import on_notification
    out = _ask()
    note = {'type': 'agent_ask', 'msg_id': out['ask_id']}
    on_notification('10202', note, push={'title': 't', 'body': 'b'})
    _settle()
    assert len(legs['pushes']) == 1


# -- the answer ------------------------------------------------------------------

def test_the_person_answers_and_the_waiting_tool_gets_it(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    got = {}
    t = threading.Thread(target=lambda: got.update(a=person_ask.wait_for_answer(ask_id, 5)))
    t.start()
    time.sleep(0.1)
    body, code = person_ask.answer_ask(ask_id, {'xids': ['ab' * 32]}, '10202')
    t.join(timeout=5)
    _settle()
    assert code == 200 and body['status'] == 'ok'
    assert got['a'] == {'xids': ['ab' * 32]}
    assert person_ask.answer_of(ask_id) == {'xids': ['ab' * 32]}
    assert _row(ask_id)['is_read'] is True
    reads = [d for t_, d, _u in legs['events']
             if t_ == 'chat.social' and d.get('type') == 'notification.read']
    assert reads and reads[-1]['ids'] == [ask_id]


def test_only_the_person_asked_may_answer(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    for who in ('777', None, ''):
        body, code = person_ask.answer_ask(ask_id, {'xids': []}, who)
        assert code == 403, who
    assert person_ask.answer_of(ask_id) is None
    assert _row(ask_id)['is_read'] is False


def test_the_first_answer_wins(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component={'type': 'approval', 'action': 'post_the_reel',
                             'description': 'Post today\'s reel?'})['ask_id']
    assert person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')[1] == 200
    body, code = person_ask.answer_ask(ask_id, {'decision': 'deny'}, '10202')
    assert code == 409 and body['reason'] == 'already_answered'
    assert body['answer'] == {'decision': 'approve'}
    assert person_ask.answer_of(ask_id) == {'decision': 'approve'}


def test_an_unknown_ask_is_not_found(legs):
    from integrations.agent_engine import person_ask
    body, code = person_ask.answer_ask('no-such-ask', {'x': 1}, '10202')
    assert code == 404 and body['reason'] == 'not_found'


def test_an_ask_this_node_no_longer_holds_is_expired_and_its_card_dropped(legs):
    """After a restart the waiting tool is gone, but the card is still on her
    devices: her answer is told it came too late, and every surface drops it."""
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    with person_ask._lock:
        person_ask._asks.clear()          # what a restart leaves
    body, code = person_ask.answer_ask(ask_id, {'xids': []}, '777')
    assert code == 404                    # someone else learns nothing
    assert _row(ask_id)['is_read'] is False
    body, code = person_ask.answer_ask(ask_id, {'xids': []}, '10202')
    assert code == 410 and body['reason'] == 'expired'
    assert _row(ask_id)['is_read'] is True
    reads = [d for t_, d, _u in legs['events']
             if t_ == 'chat.social' and d.get('type') == 'notification.read']
    assert reads and reads[-1]['ids'] == [ask_id]


def test_an_ask_whose_row_cannot_be_written_is_not_filed(legs, monkeypatch):
    from integrations.agent_engine import person_ask
    from integrations.social.services import NotificationService

    def locked(*_a, **_k):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(NotificationService, 'create', staticmethod(locked))
    before = set(person_ask._asks)
    out = _ask()
    assert out == {'ok': False, 'ask_id': None, 'refused': 'not_filed', 'card': False}
    assert set(person_ask._asks) == before
    assert legs['cards'] == [] and legs['pushes'] == []


def test_an_answer_must_be_an_object(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    for bad in ('yes', ['a'], None, 3):
        body, code = person_ask.answer_ask(ask_id, bad, '10202')
        assert code == 400 and body['reason'] == 'bad_answer', bad
    assert person_ask.answer_of(ask_id) is None


def test_no_answer_in_time_leaves_the_ask_open(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    t0 = time.monotonic()
    assert person_ask.wait_for_answer(ask_id, 0.2) is None
    assert time.monotonic() - t0 < 2
    assert _row(ask_id)['is_read'] is False
    assert person_ask.answer_ask(ask_id, {'xids': []}, '10202')[1] == 200


# -- what is never asked this way ---------------------------------------------------

def test_a_secret_field_is_refused_and_nothing_leaves(legs):
    form = {'type': 'form', 'title': 'Your Instagram login',
            'fields': [{'name': 'user', 'type': 'text'},
                       {'name': 'password', 'type': 'password', 'secret': True}],
            'action': '/x'}
    out = _ask(component=form)
    _settle()
    assert out['ok'] is False and out['refused'] == 'secrets_go_to_the_vault'
    assert out['ask_id'] is None
    assert legs['pushes'] == [] and legs['cards'] == []


def test_an_unknown_component_type_is_refused(legs):
    out = _ask(component={'type': 'definitely_not_a_component'})
    assert out['ok'] is False and out['refused'] == 'unknown_type'
    assert legs['cards'] == [] and legs['pushes'] == []


def test_an_ask_with_no_person_is_refused(legs):
    out = _ask(user_id='')
    assert out['ok'] is False and out['refused'] == 'no_person'


def test_file_request_is_a_liquid_ui_component_with_a_typed_contract():
    from integrations.agent_engine.liquid_ui_service import (
        COMPONENT_TYPES, validate_component)
    assert 'file_request' in COMPONENT_TYPES
    entry = COMPONENT_TYPES['file_request']
    errors = [i for i in validate_component('file_request', dict(CLIPS), entry)
              if i['level'] == 'error']
    assert errors == []
    missing = [i for i in validate_component(
        'file_request', {'type': 'file_request', 'title': 'x'}, entry)
        if i['level'] == 'error']
    assert [i['path'] for i in missing] == ['purpose']


# -- the tool every agent has -----------------------------------------------------

def _tool():
    from integrations.agent_engine import person_ask
    name, desc, func = person_ask.ask_person_tool('10202', '342')
    assert name == 'ask_person' and 'any of their devices' in desc
    return func


def test_the_tool_returns_the_answer_when_it_comes_in_time(legs):
    from integrations.agent_engine import person_ask
    func = _tool()

    def answer_soon():
        for _ in range(50):
            if legs['cards']:
                person_ask.answer_ask(legs['cards'][-1][1]['ask_id'],
                                  {'xids': ['cd' * 32]}, '10202')
                return
            time.sleep(0.05)

    threading.Thread(target=answer_soon).start()
    reply = json.loads(func(json.dumps(CLIPS), 5))
    assert reply['answered'] is True and reply['answer'] == {'xids': ['cd' * 32]}


def test_the_tool_holds_the_ask_when_the_person_has_not_answered(legs):
    from integrations.agent_engine import person_ask
    func = _tool()
    reply = json.loads(func(json.dumps(CLIPS), 0))
    assert reply['answered'] is False and reply['status'] == 'waiting_for_person'
    ask_id = reply['ask_id']
    assert person_ask.answer_ask(ask_id, {'xids': ['ef' * 32]}, '10202')[1] == 200
    later = json.loads(func('', 0, ask_id))
    assert later['answered'] is True and later['answer'] == {'xids': ['ef' * 32]}


def test_the_tool_reports_a_refusal_instead_of_asking(legs):
    func = _tool()
    reply = json.loads(func(json.dumps({'type': 'nope'}), 0))
    assert reply['answered'] is False and reply['refused'] == 'unknown_type'
    assert legs['cards'] == []


def test_the_tool_never_waits_longer_than_its_ceiling(legs, monkeypatch):
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(person_ask, 'MAX_WAIT_S', 0.2)
    func = _tool()
    t0 = time.monotonic()
    reply = json.loads(func(json.dumps(CLIPS), 3600))
    assert reply['answered'] is False
    assert time.monotonic() - t0 < 2
