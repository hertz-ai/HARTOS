"""A person's phone starts and ends a voice call with their agent on this
desktop, over its device link (integrations.social.phone_call).

The social call routes accept only tokens this node signed, so a phone's
device token can start nothing there.  The phone asks on the 'tunnel'
channel, beside its LiveKit signal tunnel, as the person its link was
admitted for, and gets back the call and its room token.

Driven through the link's real receive loop (channel policy, request
threading, replies), against the real social database (migrations), the
real conversation, grant and call services and the real agent bridge.
LiveKit's token signing is faked at the SDK boundary only: the test venv
has no livekit-api.
"""
import os
import sys
import threading
import time
import types
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager, reset_link_manager  # noqa: E402
from tests.unit.test_livekit_link_tunnel import WAIT_S, LinkSocket  # noqa: E402


def _signer():
    """livekit-api's token surface: the JWT names its identity and room."""
    api = types.ModuleType('livekit.api')

    class _Token:
        def __init__(self, key, secret):
            self.identity, self.room = None, None

        def with_identity(self, identity):
            self.identity = identity
            return self

        def with_grants(self, grants):
            self.room = grants.room
            return self

        def with_metadata(self, metadata):
            return self

        def with_ttl(self, ttl):
            return self

        def to_jwt(self):
            return f'jwt:{self.identity}:{self.room}'

    api.AccessToken = _Token
    api.VideoGrants = lambda **k: types.SimpleNamespace(**k)
    return api


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import migrations
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    migrations.run_migrations()
    session = models_mod.get_db()
    yield session
    session.close()
    try:
        models_mod.get_engine().dispose()
    finally:
        models_mod._engine = None
        models_mod._SessionLocal = None


@pytest.fixture(autouse=True)
def desktop(monkeypatch, db):
    """A flat desktop that serves LiveKit on its loopback, with calls on."""
    monkeypatch.setenv('HEVOLVE_FLAG_CALLS_V1', 'true')
    monkeypatch.setenv('LIVEKIT_AUTOSTART', '1')
    monkeypatch.setenv('LIVEKIT_URL', 'ws://127.0.0.1:7880')
    monkeypatch.setenv('LIVEKIT_API_KEY', 'test-key')
    monkeypatch.setenv('LIVEKIT_API_SECRET', 's' * 32)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)
    monkeypatch.delenv('HEVOLVE_MASTER_PRIVATE_KEY', raising=False)
    from integrations.social import livekit_service
    monkeypatch.setattr(livekit_service, 'livekit_api', _signer())
    monkeypatch.setattr(livekit_service, '_HAS_LIVEKIT_SDK', True)
    reset_link_manager()
    from integrations.social import livekit_link
    assert livekit_link.install()
    yield
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.shutdown_all()
    livekit_link.close_all()
    reset_link_manager()


def _user(db, user_type='human', owner_id=None):
    from integrations.social.models import User
    u = User(id=str(uuid.uuid4()), username=f'{user_type}_{uuid.uuid4().hex[:8]}',
             display_name=user_type.title(),
             email=f'{uuid.uuid4().hex[:8]}@x.test', password_hash='x:y',
             user_type=user_type)
    if owner_id is not None:
        u.owner_id = owner_id
    db.add(u)
    db.commit()
    return u.id


def _phone(user_id, kind='device'):
    """A live link from a phone signed in as ``user_id``, its receive loop
    running over a LinkSocket."""
    link = PeerLink(f'{kind}-{uuid.uuid4().hex[:6]}', 'relay:conv-1',
                    TrustLevel.SAME_USER)
    link.kind = kind
    link.user_id = user_id if kind == 'device' else ''
    ws = LinkSocket()
    link._ws = ws
    link._state = LinkState.CONNECTED
    mgr = get_link_manager()
    mgr._apply_channel_handlers(link)
    mgr._links[link.peer_id] = link
    threading.Thread(target=link._receive_loop, daemon=True).start()
    return ws


def _ask(ws, frame):
    ws.frame(frame, rq=True, msg_id=f'rq-{uuid.uuid4().hex[:6]}')
    return ws.next_out()['d']


def _open(ws, agent_id, rid='c1'):
    return _ask(ws, {'type': 'call_open', 'id': rid, 'agent_id': agent_id})


def _no_answer(ws, frame, wait_s=1.5):
    ws.frame(frame, rq=True, msg_id=f'rq-{uuid.uuid4().hex[:6]}')
    time.sleep(wait_s)
    return ws.out.empty()


class TestAPhoneStartsACallWithItsAgent:

    def test_the_phone_gets_the_call_and_its_own_room_token(self, db):
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        reply = _open(_phone(person), agent)
        assert reply['type'] == 'call_opened' and reply['id'] == 'c1'
        call_id = reply['call_id']
        assert reply['url'] == 'ws://127.0.0.1:7880'
        assert reply['token'] == f'jwt:{person}:{call_id}'

        from integrations.social.agent_voice_bridge import AgentVoiceBridge
        from integrations.social.call_service import CallService
        call = CallService.get(db, call_id)
        assert call['parent_kind'] == 'conversation' and call['kind'] == 'voice'
        assert call['started_by'] == person and not call['ended_at']
        roster = {p['user_id']: p for p in CallService.list_participants(db, call_id)}
        assert set(roster) == {person, agent}
        assert roster[agent]['device_kind'] == 'agent_bridge'
        grant = CallService.get_active_grant(db, agent, 'conversation',
                                             call['parent_id'])
        assert grant['scope'].get('can_voice') is True
        assert [w['agent_id'] for w in AgentVoiceBridge.list_active(call_id)] == [agent]

    def test_asking_again_joins_the_same_call(self, db):
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        ws = _phone(person)
        first = _open(ws, agent, 'c1')
        second = _open(ws, agent, 'c2')
        assert second['type'] == 'call_opened' and second['id'] == 'c2'
        assert second['call_id'] == first['call_id']

    def test_a_person_who_left_the_call_is_back_in_it(self, db):
        """The call is still open (the agent is in it) after the person left
        it: asking again puts them back in its roster, not just hands them a
        token for a room they are not listed in."""
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        ws = _phone(person)
        call_id = _open(ws, agent, 'c1')['call_id']
        from integrations.social.call_service import CallService
        CallService.leave(db, call_id, person)
        assert _open(ws, agent, 'c2')['call_id'] == call_id
        active = {p['user_id'] for p in CallService.list_participants(db, call_id)}
        assert active == {person, agent}

    def test_a_grant_already_there_keeps_its_other_rights(self, db):
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        from integrations.social.call_service import CallService
        from integrations.social.conversation_service import ConversationService
        conv = ConversationService.create(db, 'dm', [agent], person)
        CallService.grant_agent(db, agent, person, 'conversation', conv['id'],
                                {'can_screen': True})
        reply = _open(_phone(person), agent)
        assert reply['type'] == 'call_opened'
        scope = CallService.get_active_grant(
            db, agent, 'conversation', conv['id'])['scope']
        assert scope == {'can_screen': True, 'can_voice': True}


class TestWhatIsRefused:

    def test_someone_elses_agent(self, db):
        person, author = _user(db), _user(db)
        agent = _user(db, 'agent', owner_id=author)
        reply = _open(_phone(person), agent)
        assert reply == {'type': 'call_refused', 'id': 'c1', 'reason': 'not_allowed',
                         'detail': "only the agent's owner can grant join"}
        from sqlalchemy import text
        assert db.execute(text("SELECT COUNT(*) FROM call_sessions "
                               "WHERE started_by = :p"), {'p': person}).scalar() == 0

    def test_a_user_who_is_not_an_agent(self, db):
        person, friend = _user(db), _user(db)
        reply = _open(_phone(person), friend)
        assert reply == {'type': 'call_refused', 'id': 'c1', 'reason': 'no_agent'}
        from sqlalchemy import text
        assert db.execute(text("SELECT COUNT(*) FROM conversations "
                               "WHERE created_by = :p"), {'p': person}).scalar() == 0

    def test_no_agent_named(self, db):
        reply = _ask(_phone(_user(db)), {'type': 'call_open', 'id': 'c1'})
        assert reply == {'type': 'call_refused', 'id': 'c1', 'reason': 'bad_request'}

    def test_calls_switched_off_on_this_desktop(self, db, monkeypatch):
        monkeypatch.setenv('HEVOLVE_FLAG_CALLS_V1', 'false')
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        reply = _open(_phone(person), agent)
        assert reply == {'type': 'call_refused', 'id': 'c1', 'reason': 'calls_off'}

    def test_a_device_link_that_names_no_person_is_not_answered(self, db):
        agent = _user(db, 'agent', owner_id=_user(db))
        ws = _phone('')
        assert _no_answer(ws, {'type': 'call_open', 'id': 'c1', 'agent_id': agent})

    def test_a_node_link_is_not_answered(self, db):
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        ws = _phone(person, kind='node')
        assert _no_answer(ws, {'type': 'call_open', 'id': 'c1', 'agent_id': agent})

    def test_a_failure_on_this_desktop_is_answered_not_dropped(self, db, monkeypatch):
        """The link logs a handler's exception at debug and sends nothing,
        so the phone would wait out its whole budget: the failure is
        answered instead."""
        from integrations.social.call_service import CallService

        def _fails(*a, **k):
            raise RuntimeError('database is locked')

        monkeypatch.setattr(CallService, 'attach_agent', staticmethod(_fails))
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        reply = _open(_phone(person), agent)
        assert reply == {'type': 'call_refused', 'id': 'c1', 'reason': 'error',
                         'detail': 'database is locked'}

    def test_no_room_token_ends_the_call_it_opened(self, db, monkeypatch):
        from integrations.social import livekit_service
        monkeypatch.setattr(livekit_service, '_HAS_LIVEKIT_SDK', False)
        monkeypatch.setattr(livekit_service, '_LIVEKIT_SDK_IMPORT_ERROR',
                            "ImportError: cannot import name 'timestamp_pb2'")
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        reply = _open(_phone(person), agent)
        assert reply['type'] == 'call_refused' and reply['reason'] == 'no_room'
        assert reply['detail'] == 'livekit-api did not import (ImportError)'
        from sqlalchemy import text
        from integrations.social.agent_voice_bridge import AgentVoiceBridge
        open_calls = db.execute(text(
            "SELECT id FROM call_sessions WHERE started_by = :p "
            "AND ended_at IS NULL"), {'p': person}).fetchall()
        assert open_calls == []
        assert AgentVoiceBridge.list_active() == []


class TestHangingUp:

    def test_the_phone_ends_its_call_and_the_agent_leaves(self, db):
        person = _user(db)
        agent = _user(db, 'agent', owner_id=person)
        ws = _phone(person)
        call_id = _open(ws, agent)['call_id']
        reply = _ask(ws, {'type': 'call_end', 'id': 'e1', 'call_id': call_id})
        assert reply == {'type': 'call_ended', 'id': 'e1', 'call_id': call_id}
        from integrations.social.agent_voice_bridge import AgentVoiceBridge
        from integrations.social.call_service import CallService
        assert CallService.get(db, call_id)['ended_at']
        assert AgentVoiceBridge.list_active(call_id) == []

    def test_a_phone_cannot_end_someone_elses_call(self, db):
        person, other = _user(db), _user(db)
        agent = _user(db, 'agent', owner_id=person)
        call_id = _open(_phone(person), agent)['call_id']
        reply = _ask(_phone(other), {'type': 'call_end', 'id': 'e1',
                                     'call_id': call_id})
        assert reply['type'] == 'call_refused' and reply['reason'] == 'not_allowed'
        from integrations.social.call_service import CallService
        assert not CallService.get(db, call_id)['ended_at']


def test_ending_a_call_stops_its_agents(db):
    """CallService.end -- the REST end route's call as well as the phone's --
    stops the agent bridges in the call.  Before, nothing outside the bridge
    module ever detached one: an ended call's agent kept its worker ticking
    and its publisher in the room."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    from integrations.social.call_service import CallService
    from integrations.social.conversation_service import ConversationService
    person = _user(db)
    agent = _user(db, 'agent', owner_id=person)
    conv = ConversationService.create(db, 'dm', [agent], person)
    CallService.grant_agent(db, agent, person, 'conversation', conv['id'],
                            {'can_voice': True})
    call = CallService.create(db, 'conversation', conv['id'], person)
    CallService.attach_agent(db, call['id'], agent)
    assert len(AgentVoiceBridge.list_active(call['id'])) == 1
    CallService.end(db, call['id'], person)
    assert AgentVoiceBridge.list_active(call['id']) == []
