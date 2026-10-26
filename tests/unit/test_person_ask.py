"""The server asks the person through Liquid UI, as a tool.

Owner, 2026-10-27: "liquid ui is asked as a tool by server", "the mechanism
shd be same agnostic of hartos consumer, liqiuid ui shd also fanout", "same
for consents via firebase shd fanout in desktop, android, wamp etc".
Design: docs/design/PERSON_ASK_LIQUID_UI_TOOL.md.

One ask is one Liquid UI card, then one Notification row created by
NotificationService.create with a push (the Firebase leg of
realtime.on_notification), all under one id.  The card goes first: its gates
(the kill switch, a halted hive, unsafe content, the allowlist) decide
whether anything reaches the person at all.  The person answers on any
surface; the first answer wins; the asking agent gets it; ``ask.answered``
tells every surface to drop the card.  Reading the ask in the bell is not an
answer.

Real NotificationService, realtime fan-out and in-memory social DB, and for
the card a real LiquidUIService (no server) where the test is about the card.
The boundaries are recorded: core.fcm_sync.send_fcm_push (Google / central's
relay), realtime.publish_event (WAMP) and, in the ``legs`` tests,
liquid_ui_service.push_agent_ui.  No grep tests.

    python -m pytest tests/unit/test_person_ask.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

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
POST = {'type': 'approval', 'action': 'post_the_reel',
        'description': 'Post today\'s reel?'}


def _rmtree(root):
    """The test venv's shutil.rmtree breaks (os._walk_symlinks_as_files),
    so the temp dirs are removed by hand."""
    for top, dirs, files in os.walk(root, topdown=False):
        for f in files:
            os.remove(os.path.join(top, f))
        for d in dirs:
            os.rmdir(os.path.join(top, d))
    os.rmdir(root)


def _settle():
    for t in [t for t in threading.enumerate() if t.name == 'notify-push']:
        t.join(timeout=5)


def _people():
    Base.metadata.create_all(get_engine())
    for uid in ('10202', '777'):
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'p{uid}', display_name=f'P {uid}',
                            user_type='human', api_token=f'tok-{uid}'))


@pytest.fixture(autouse=True)
def _no_open_asks():
    """Each test starts with no ask open (the cap counts open asks)."""
    from integrations.agent_engine import person_ask
    with person_ask._lock:
        person_ask._asks.clear()
    yield
    with person_ask._lock:
        person_ask._asks.clear()


def _record(monkeypatch, got):
    monkeypatch.setenv('HEVOLVE_START_BACKGROUND_SERVICES', '1')
    import core.fcm_sync as fcm
    import integrations.social.realtime as rt

    def fake_send(user_id, title, body, data=None, **_k):
        got['pushes'].append((str(user_id), title, body, dict(data or {})))
        return True

    monkeypatch.setattr(fcm, 'send_fcm_push', fake_send)
    monkeypatch.setattr(rt, 'publish_event',
                        lambda topic, data, user_id=None: got['events'].append(
                            (topic, dict(data), user_id)))
    pushed = getattr(rt, '_PUSHED', None)
    if pushed is not None:
        pushed.clear()


@pytest.fixture
def legs(monkeypatch):
    """What left the node for the person: Firebase pushes, WAMP events and
    Liquid UI cards (push_agent_ui recorded)."""
    _people()
    got = {'pushes': [], 'events': [], 'cards': []}
    _record(monkeypatch, got)
    import integrations.agent_engine.liquid_ui_service as lui
    monkeypatch.setattr(lui, 'push_agent_ui',
                        lambda agent_id, card, user_id=None: got['cards'].append(
                            (agent_id, dict(card), user_id)) or True)
    monkeypatch.setattr(lui, 'withdraw_agent_ui', lambda msg_id: 0, raising=False)
    yield got
    _settle()


def _bare_shell():
    """A LiquidUIService with only the state the A2UI push touches (the
    pattern of tests/probes/test_os_pillars._bare_liquid_ui): the REAL
    allowlist, kill switch, hive halt, rate cap and XSS gate."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    svc = LiquidUIService.__new__(LiquidUIService)
    svc.a2ui_enabled = True
    svc._agent_components = {}
    svc._lock = threading.Lock()
    svc._ui_event_cv = threading.Condition()
    svc._a2ui_buckets = {}
    svc._custom_component_types = {}
    svc._data_dir = tempfile.mkdtemp(prefix='person_ask_ui_')
    return svc


@pytest.fixture
def shell(monkeypatch):
    """The real card path: push_agent_ui -> a registered LiquidUIService."""
    from core.platform.registry import get_registry, reset_registry
    _people()
    got = {'pushes': [], 'events': []}
    _record(monkeypatch, got)
    reset_registry()
    svc = _bare_shell()
    get_registry().register('LiquidUIService', lambda: svc)
    got['svc'] = svc
    yield got
    _settle()
    reset_registry()
    _rmtree(svc._data_dir)


def _row(ask_id):
    with db_session(commit=False) as db:
        n = db.query(Notification).filter_by(id=ask_id).first()
        return None if n is None else {
            'user_id': n.user_id, 'type': n.type, 'is_read': bool(n.is_read),
            'target_type': n.target_type, 'target_id': n.target_id,
            'message': n.message}


def _rows():
    with db_session(commit=False) as db:
        return db.query(Notification).filter_by(type='agent_ask').count()


def _ask(**kw):
    from integrations.agent_engine import person_ask
    args = {'user_id': '10202', 'agent_id': '342', 'component': dict(CLIPS),
            'prompt_id': '342', 'title': 'Your Reel Studio agent needs your clips',
            'body': 'Pick the clips for your reel.'}
    args.update(kw)
    return person_ask.ask(args.pop('user_id'), args.pop('agent_id'),
                          args.pop('component'), **args)


def _answered_events(legs, ask_id):
    return [d for t, d, _u in legs['events']
            if t == 'chat.social' and d.get('type') == 'ask.answered'
            and d.get('ask_id') == ask_id]


# -- one ask, every leg, one id ------------------------------------------------

def test_an_ask_is_one_card_one_row_and_one_push_under_one_id(legs):
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
    # central's relay keeps no request_id of a push that is not a consent
    # (fcm_sync._CENTRAL_OWNED_PUSH_KEYS), so the ask's id travels as its own key
    assert data['ask_id'] == ask_id
    # an anonymous-router topic is never where an answer comes from
    assert 'topic_reply' not in data
    assert data['component_type'] == 'file_request'
    assert len(legs['cards']) == 1
    agent_id, card, card_user = legs['cards'][0]
    assert (agent_id, card_user) == ('342', '10202')
    assert card['ask_id'] == ask_id and card['msg_id'] == ask_id
    assert card['type'] == 'file_request' and card['purpose'] == 'reel_source'
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


def test_the_bell_names_an_ask_in_words():
    from integrations.social.models import Notification
    assert Notification(type='agent_ask').title_for_humans() == \
        'Your agent asks you something'


# -- the card goes first: its gates stop everything -------------------------------

def test_the_card_is_stored_and_sent_under_the_asks_id(shell):
    out = _ask()
    _settle()
    assert out['ok'] is True and out['card'] is True
    stored = shell['svc']._agent_components['342']
    assert [c['msg_id'] for c in stored] == [out['ask_id']]
    assert stored[0]['ask_id'] == out['ask_id']
    owner_cards = [d['component'] for t, d, _u in shell['events']
                   if t == 'chat.social' and d.get('type') == 'agent_ui_update']
    assert [c['msg_id'] for c in owner_cards] == [out['ask_id']]


@pytest.mark.parametrize('stop', ['hive_halted', 'a2ui_off', 'unsafe_title'])
def test_a_card_the_gates_refuse_sends_no_row_and_no_push(shell, stop):
    comp = dict(CLIPS)
    if stop == 'a2ui_off':
        shell['svc'].a2ui_enabled = False
    if stop == 'unsafe_title':
        comp['title'] = '<script>alert(1)</script>'
    halted = stop == 'hive_halted'
    before = _rows()
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=halted):
        out = _ask(component=comp, title='')
    _settle()
    assert out == {'ok': False, 'ask_id': None, 'refused': 'not_shown', 'card': False}
    assert shell['pushes'] == []
    assert _rows() == before
    assert shell['svc']._agent_components.get('342', []) == []


def test_an_answered_card_leaves_the_shells_store(shell):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST), title='Post it?')['ask_id']
    assert shell['svc']._agent_components['342']
    assert person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')[1] == 200
    assert shell['svc']._agent_components['342'] == []


def test_a_row_that_cannot_be_written_takes_the_card_back(shell, monkeypatch):
    from integrations.agent_engine import person_ask
    from integrations.social.services import NotificationService

    def locked(*_a, **_k):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(NotificationService, 'create', staticmethod(locked))
    out = _ask()
    assert out == {'ok': False, 'ask_id': None, 'refused': 'not_filed', 'card': False}
    assert person_ask._asks == {}
    assert shell['svc']._agent_components['342'] == []
    assert shell['pushes'] == []


def test_an_agents_registered_component_type_can_be_asked(shell):
    """The card gate is the one allowlist: a type an agent registered at
    runtime is asked like a builtin."""
    svc = shell['svc']
    svc._custom_component_types['clip_picker'] = {
        'props': ['title'], 'attributes': {'title': 'str'}, 'events': []}
    out = _ask(component={'type': 'clip_picker', 'title': 'Pick one'}, title='')
    assert out['ok'] is True, out


# -- the answer ------------------------------------------------------------------

def test_the_person_answers_and_the_waiting_tool_gets_it(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    got = {}
    t = threading.Thread(target=lambda: got.update(a=person_ask.wait_for_answer(ask_id, 5)))
    t.start()
    time.sleep(0.1)
    body, code = person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')
    t.join(timeout=5)
    _settle()
    assert code == 200 and body['status'] == 'ok'
    assert got['a'] == {'decision': 'approve'}
    assert person_ask.ask_state(ask_id, '10202') == {
        'status': 'answered', 'answer': {'decision': 'approve'}}
    assert _row(ask_id)['is_read'] is True
    assert len(_answered_events(legs, ask_id)) == 1


def test_reading_the_ask_in_the_bell_is_not_an_answer(legs):
    """Mark all read (or a click on the ask's row) flips the row; the ask
    stays open, nothing tells the surfaces it was answered, and her answer
    still counts."""
    from integrations.agent_engine import person_ask
    from integrations.social.services import NotificationService
    ask_id = _ask(component=dict(POST))['ask_id']
    with db_session() as db:
        NotificationService.mark_all_read(db, '10202')
    assert _answered_events(legs, ask_id) == []
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'
    assert person_ask.answer_ask(ask_id, {'decision': 'deny'}, '10202')[1] == 200
    assert len(_answered_events(legs, ask_id)) == 1


def test_only_the_person_asked_may_answer(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    for who in ('777', None, ''):
        body, code = person_ask.answer_ask(ask_id, {'decision': 'approve'}, who)
        assert code == 403, who
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'
    assert _row(ask_id)['is_read'] is False
    assert _answered_events(legs, ask_id) == []


def test_the_first_answer_wins(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    assert person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')[1] == 200
    body, code = person_ask.answer_ask(ask_id, {'decision': 'deny'}, '10202')
    assert code == 409 and body['reason'] == 'already_answered'
    assert body['answer'] == {'decision': 'approve'}
    assert person_ask.ask_state(ask_id, '10202')['answer'] == {'decision': 'approve'}


def test_an_unknown_ask_is_not_found(legs):
    from integrations.agent_engine import person_ask
    body, code = person_ask.answer_ask('no-such-ask', {'x': 1}, '10202')
    assert code == 404 and body['reason'] == 'not_found'


def test_an_ask_this_node_no_longer_holds_is_expired_and_its_card_dropped(legs):
    """After a restart the waiting tool is gone, but the card is still on her
    devices: her answer is told it came too late, and every surface drops it."""
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    with person_ask._lock:
        person_ask._asks.clear()          # what a restart leaves
    body, code = person_ask.answer_ask(ask_id, {'decision': 'approve'}, '777')
    assert code == 404                    # someone else learns nothing
    assert _row(ask_id)['is_read'] is False
    body, code = person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')
    assert code == 410 and body['reason'] == 'expired'
    assert _row(ask_id)['is_read'] is True
    assert len(_answered_events(legs, ask_id)) == 1


def test_an_answer_must_be_a_small_shallow_object(legs, monkeypatch):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    deep = {}
    node = deep
    for _ in range(3000):
        node['a'] = {}
        node = node['a']
    for bad in ('yes', ['a'], None, 3, {'note': 'x' * (person_ask.MAX_ANSWER_BYTES + 1)},
                deep, {'a': {'b': {'c': {'d': {'e': {'f': {'g': {'h': {'i': 1}}}}}}}}}):
        body, code = person_ask.answer_ask(ask_id, bad, '10202')
        assert code == 400 and body['reason'] == 'bad_answer', str(bad)[:40]
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'


def test_no_answer_in_time_leaves_the_ask_open(legs):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    t0 = time.monotonic()
    assert person_ask.wait_for_answer(ask_id, 0.2) is None
    assert time.monotonic() - t0 < 2
    assert _row(ask_id)['is_read'] is False
    assert person_ask.answer_ask(ask_id, {'decision': 'later'}, '10202')[1] == 200


def test_what_the_agent_reads_back(legs):
    """Open, answered, gone (a restart, or kept past ANSWERED_KEEP_S), and
    nothing at all for an agent of another person."""
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(POST))['ask_id']
    assert person_ask.ask_state(ask_id, '10202') == {'status': 'open', 'answer': None}
    assert person_ask.ask_state(ask_id, '777') == {'status': 'not_found', 'answer': None}
    person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')
    assert person_ask.ask_state(ask_id, '777') == {'status': 'not_found', 'answer': None}
    with person_ask._lock:
        person_ask._asks[ask_id].answered_at -= person_ask.ANSWERED_KEEP_S + 1
    _ask(component={'type': 'approval', 'action': 'another'})   # prunes on the way in
    assert person_ask.ask_state(ask_id, '10202') == {'status': 'expired', 'answer': None}


# -- never more than a few at once ---------------------------------------------------

def test_the_same_question_asked_again_is_the_same_ask(legs):
    before = _rows()
    first = _ask(component=dict(POST))
    again = _ask(component=dict(POST))
    _settle()
    assert again['ok'] is True and again['ask_id'] == first['ask_id']
    assert again.get('reused') is True
    assert len(legs['pushes']) == 1 and len(legs['cards']) == 1
    assert _rows() == before + 1


def test_a_person_has_at_most_a_few_open_asks(legs):
    from integrations.agent_engine import person_ask
    ids = [_ask(component={'type': 'approval', 'action': f'a{i}'})['ask_id']
           for i in range(person_ask.MAX_OPEN_PER_PERSON)]
    assert all(ids)
    over = _ask(component={'type': 'approval', 'action': 'one_more'})
    _settle()
    assert over == {'ok': False, 'ask_id': None, 'refused': 'too_many_open_asks',
                    'card': False}
    assert len(legs['pushes']) == person_ask.MAX_OPEN_PER_PERSON
    # another person is not held back by hers
    assert _ask(user_id='777', component={'type': 'approval', 'action': 'x'})['ok']
    # answering one makes room
    person_ask.answer_ask(ids[0], {'decision': 'approve'}, '10202')
    assert _ask(component={'type': 'approval', 'action': 'one_more'})['ok'] is True


# -- what is never asked or answered this way ----------------------------------------

@pytest.mark.parametrize('field', [
    {'name': 'password', 'type': 'password', 'secret': True},
    {'name': 'pw', 'type': 'PASSWORD'},
    {'name': 'pw', 'input_type': 'password'},
    {'name': 'pw', 'secret': 'true'},
    {'name': 'password', 'type': 'text', 'label': 'Your Instagram password'},
    {'name': 'api_key', 'type': 'text', 'label': 'Paste your OpenAI key'},
])
def test_a_component_that_asks_for_a_secret_is_refused(legs, field):
    form = {'type': 'form', 'title': 'Your login',
            'fields': [{'name': 'user', 'type': 'text'}, field], 'action': '/x'}
    out = _ask(component=form)
    _settle()
    assert out['ok'] is False and out['refused'] == 'secrets_go_to_the_vault'
    assert legs['pushes'] == [] and legs['cards'] == []


@pytest.mark.parametrize('answer', [
    {'password': 'hunter2-hunter2'},
    {'api_key': 'sk-proj-abcdefghijklmnopqrstuvwxyz0123456789'},
    {'note': 'my key is sk-proj-abcdefghijklmnopqrstuvwxyz0123456789'},
])
def test_an_answer_that_carries_a_secret_is_refused(legs, answer):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component={'type': 'form', 'title': 'Tell me',
                             'fields': [{'name': 'note', 'type': 'text'}],
                             'action': '/x'})['ask_id']
    body, code = person_ask.answer_ask(ask_id, answer, '10202')
    assert code == 400 and body['reason'] == 'secrets_go_to_the_vault'
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'


def test_an_ask_with_no_person_is_refused(legs):
    out = _ask(user_id='')
    assert out['ok'] is False and out['refused'] == 'no_person'


def test_an_ask_that_is_not_a_component_is_refused(legs):
    out = _ask(component='post it?')
    assert out['ok'] is False and out['refused'] == 'not_an_object'
    assert legs['cards'] == []


# -- file_request: the picks are the agent's files ------------------------------------

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


@pytest.mark.parametrize('comp, prompt, reason', [
    (dict(CLIPS, purpose='song'), '342', 'unknown_purpose'),
    (dict(CLIPS, purpose='reel'), '342', 'unknown_purpose'),
    (dict(CLIPS), None, 'no_agent'),
])
def test_a_file_request_the_store_could_not_take_is_refused(legs, comp, prompt, reason):
    if prompt is None:
        comp.pop('prompt_id')
    out = _ask(component=comp, prompt_id=prompt)
    assert out['ok'] is False and out['refused'] == reason
    assert legs['cards'] == [] and legs['pushes'] == []


def test_a_logo_is_asked_for_one_file(legs):
    _ask(component=dict(CLIPS, purpose='logo', accept='image', max_files=4))
    assert legs['cards'][-1][1]['max_files'] == 1


@pytest.fixture
def store(monkeypatch):
    """A real user file store in a temp dir (no disk margin: the test files
    are bytes), behind the one binding rule (core.chat_attachments)."""
    import core.chat_attachments as ca
    from core.user_file_store import UserFileStore
    root = tempfile.mkdtemp(prefix='person_ask_store_')
    st = UserFileStore(root, policy={'free_margin': 0})
    monkeypatch.setattr(ca, 'get_user_file_store', lambda: st)
    yield st
    _rmtree(root)


def _sent(st, user, data, purpose='reel_source', name='look.mp4'):
    out = st.put_file(user, data, name=name, mime='video/mp4', purpose=purpose)
    assert out.get('xid'), out
    return out['xid']


def test_her_picks_are_bound_to_the_agent_that_asked(legs, store):
    from core.chat_attachments import latest_inbound
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    xids = [_sent(store, '10202', b'clip one'), _sent(store, '10202', b'clip two')]
    body, code = person_ask.answer_ask(ask_id, {'xids': xids}, '10202')
    assert code == 200, body
    bound = latest_inbound('10202', '342', request_id=ask_id)
    assert [f['xid'] for f in bound] == xids
    assert person_ask.ask_state(ask_id, '10202')['answer'] == {'xids': xids}


@pytest.mark.parametrize('case', ['not_held', 'another_persons', 'wrong_purpose',
                                  'too_many', 'extra_key', 'not_an_xid', 'none'])
def test_picks_the_store_does_not_hold_for_her_are_refused(legs, store, case):
    from integrations.agent_engine import person_ask
    ask_id = _ask(component=dict(CLIPS, max_files=2))['ask_id']
    mine = _sent(store, '10202', b'clip one')
    answer = {
        'not_held': {'xids': [mine, 'ab' * 32]},
        'another_persons': {'xids': [_sent(store, '777', b'his clip')]},
        'wrong_purpose': {'xids': [_sent(store, '10202', b'a doc', 'chat_attachment')]},
        'too_many': {'xids': [mine, _sent(store, '10202', b'2'),
                              _sent(store, '10202', b'3')]},
        'extra_key': {'xids': [mine], 'note': 'hi'},
        'not_an_xid': {'xids': ['not-an-xid']},
        'none': {'xids': []},
    }[case]
    body, code = person_ask.answer_ask(ask_id, answer, '10202')
    assert code == 400, body
    assert body['reason'] in ('files_not_received', 'bad_answer')
    assert person_ask.ask_state(ask_id, '10202')['status'] == 'open'


def test_a_file_ask_names_its_purpose_to_the_door_that_takes_the_files(legs):
    """Nunba's /api/chat-sync/files/push takes a file for an ask only while
    that ask is open, for that person, and for the purpose the ask names."""
    from integrations.agent_engine import person_ask
    ask_id = _ask()['ask_id']
    assert person_ask.open_file_request(ask_id, '10202') == {
        'purpose': 'reel_source', 'prompt_id': '342', 'max_files': 5}
    assert person_ask.open_file_request(ask_id, '777') is None
    assert person_ask.open_file_request('nope', '10202') is None
    other = _ask(component=dict(POST))['ask_id']
    assert person_ask.open_file_request(other, '10202') is None
    person_ask.answer_ask(other, {'decision': 'approve'}, '10202')
    with person_ask._lock:
        person_ask._asks[ask_id].answered_at = time.time()
    assert person_ask.open_file_request(ask_id, '10202') is None


# -- the tool every agent has -----------------------------------------------------

def _tool(user='10202'):
    from integrations.agent_engine import person_ask
    name, desc, func = person_ask.ask_person_tool(user, '342')
    assert name == 'ask_person' and 'any of their devices' in desc
    return func


def test_every_agent_has_the_tool_on_its_main_leg():
    from core.agent_tool_menu import main_leg_core_tools
    from core.agent_tools import build_core_tool_closures
    ctx = {'user_id': '10202', 'prompt_id': '342', 'agent_data': {},
           'helper_fun': MagicMock(), 'user_prompt': '10202_342',
           'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
           'send_message_to_user1': lambda *a, **k: None,
           'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
           'save_conversation_db': lambda *a, **k: None}
    tools = build_core_tool_closures(ctx)
    assert 'ask_person' in [t[0] for t in main_leg_core_tools(tools)]


def test_the_tool_returns_the_answer_when_it_comes_in_time(legs):
    from integrations.agent_engine import person_ask
    func = _tool()

    def answer_soon():
        for _ in range(50):
            if legs['cards']:
                person_ask.answer_ask(legs['cards'][-1][1]['ask_id'],
                                      {'decision': 'approve'}, '10202')
                return
            time.sleep(0.05)

    threading.Thread(target=answer_soon).start()
    reply = json.loads(func(json.dumps(POST), 5))
    assert reply['answered'] is True and reply['answer'] == {'decision': 'approve'}


def test_the_tool_holds_the_ask_when_the_person_has_not_answered(legs):
    from integrations.agent_engine import person_ask
    func = _tool()
    reply = json.loads(func(json.dumps(POST), 0))
    assert reply['answered'] is False and reply['status'] == 'waiting_for_person'
    ask_id = reply['ask_id']
    assert person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')[1] == 200
    later = json.loads(func('', 0, ask_id))
    assert later['answered'] is True and later['answer'] == {'decision': 'approve'}


def test_the_tool_says_when_an_ask_is_gone(legs):
    from integrations.agent_engine import person_ask
    func = _tool()
    ask_id = json.loads(func(json.dumps(POST), 0))['ask_id']
    with person_ask._lock:
        person_ask._asks.clear()          # a restart
    reply = json.loads(func('', 0, ask_id))
    assert reply['answered'] is False and reply['status'] == 'expired'
    # another person's agent learns nothing of hers
    ask_id = json.loads(func(json.dumps(POST), 0))['ask_id']
    person_ask.answer_ask(ask_id, {'decision': 'approve'}, '10202')
    theirs = json.loads(_tool('777')('', 0, ask_id))
    assert theirs['answered'] is False and theirs['status'] == 'not_found'
    assert 'answer' not in theirs or theirs['answer'] is None


def test_the_tool_reports_a_refusal_instead_of_asking(legs):
    func = _tool()
    reply = json.loads(func('not json', 0))
    assert reply['answered'] is False and reply['refused'] == 'not_an_object'
    assert legs['cards'] == []


def test_the_tool_never_waits_longer_than_its_ceiling(legs, monkeypatch):
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(person_ask, 'MAX_WAIT_S', 0.2)
    func = _tool()
    t0 = time.monotonic()
    reply = json.loads(func(json.dumps(POST), 3600))
    assert reply['answered'] is False
    assert time.monotonic() - t0 < 2
