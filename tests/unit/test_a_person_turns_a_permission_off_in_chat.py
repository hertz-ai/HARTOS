"""A person tells an agent in chat to stop using a permission, and it stops
for that agent only.

Owner, 2026-11-21: a consent an agent asked for once is kept "unless
explicitly asked to be turned off by user or user consents off in nunba
admin for that agent".  No agent tool could end a consent: Request_Camera_Access
and Request_Screen_Access only ask.  The Turn_Off_Permission tool
(hart_intelligence_entry._turn_off_consent) answers with
consent_service.turn_off_in_chat, whose write is ConsentService.decline for
the turn's own agent, the same no as the card's "Don't allow <agent>".

hart_intelligence_entry.py cannot be imported in a unit test (LangChain and
autogen at module scope), so its wrapper and its registration are read from
source, as tests/unit/test_capability_consent_canonical.py does.

    python -m pytest tests/unit/test_a_person_turns_a_permission_off_in_chat.py -q
"""
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

from integrations.social import consent_service as cs  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402

OWNER = 'owner-chat-off'
AGENT = '88659566083'
OTHER = '70264903070'
_HIE = Path(ROOT) / 'hart_intelligence_entry.py'


@pytest.fixture(autouse=True)
def world(monkeypatch):
    Base.metadata.create_all(get_engine())
    with db_session(commit=True) as db:
        db.query(UserConsent).filter_by(user_id=OWNER).delete()
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.setattr(cs, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', lambda *a, **k: None)
    yield
    with db_session(commit=True) as db:
        db.query(UserConsent).filter_by(user_id=OWNER).delete()


def _allow_all(consent_type='camera_capture'):
    """"Allow ALL agents", given a minute ago so the order of the two answers
    never rests on the clock's resolution."""
    with db_session(commit=True) as db:
        row = ConsentService.grant_consent(db, OWNER, consent_type)
        row.granted_at = row.granted_at - timedelta(seconds=60)


def _allowed(agent_id, consent_type='camera_capture'):
    with db_session() as db:
        return ConsentService.check_consent(db, OWNER, consent_type, agent_id=agent_id)


def _rows(**kw):
    with db_session() as db:
        return [(r.agent_id, r.consent_type, bool(r.granted), r.revoked_at is not None)
                for r in db.query(UserConsent).filter_by(user_id=OWNER, **kw).all()]


# -- the switch ---------------------------------------------------------------

def test_turn_the_camera_off_for_this_agent_ends_it_for_this_agent_only():
    _allow_all()
    reply = cs.turn_off_in_chat('turn the camera off for this agent', AGENT,
                                turn_user=OWNER)
    assert _allowed(AGENT) is False, f'still allowed after: {reply}'
    assert _allowed(OTHER) is True, 'another agent lost the camera'
    assert _allowed(None) is True, 'allow-all itself was ended'
    assert _rows(agent_id=AGENT) == [(AGENT, 'camera_capture', False, True)], (
        'the no is not on file as this agent\'s own declined row')
    assert _rows(agent_id=OTHER) == []


def test_the_answer_tells_the_model_what_happened_and_what_to_say():
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Done: camera is now turned off for this agent only')
    assert 'other agents are not changed' in reply
    assert 'Do not use camera again' in reply
    assert 'Tell the person' in reply and 'Privacy settings' in reply


def test_the_agent_is_then_told_no_when_it_asks_again():
    _allow_all()
    cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert cs.ask_owner('camera_capture', 'look', AGENT) == cs.OWNER_SAID_NO
    assert cs.ask_owner('camera_capture', 'look', OTHER) == cs.OWNER_ALLOWED


@pytest.mark.parametrize('said, consent_type', [
    ('stop using my camera', 'camera_capture'),
    ('Camera', 'camera_capture'),
    ('camera_capture', 'camera_capture'),
    ('the screen please', 'screen_capture'),
    ('screen capture', 'screen_capture'),
    ('computer control', 'computer_control'),
    ('computer_control', 'computer_control'),
    ('Data Access', 'data_access'),
])
def test_the_words_a_person_uses_name_the_permission(said, consent_type):
    assert cs.chat_off_type(said) == consent_type
    _allow_all(consent_type)
    word = {t: w for w, t in cs.CHAT_OFF_TYPES.items()}[consent_type]
    reply = cs.turn_off_in_chat(said, AGENT, turn_user=OWNER)
    assert reply.startswith(f'Done: {word} is now turned off for this agent only'), reply
    assert _allowed(AGENT, consent_type) is False
    assert _allowed(OTHER, consent_type) is True


def test_only_the_named_permission_is_turned_off():
    _allow_all('camera_capture')
    _allow_all('screen_capture')
    cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert _allowed(AGENT, 'screen_capture') is True


def test_an_agents_own_grant_is_ended_too():
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'computer_control', agent_id=AGENT)
    assert cs.turn_off_for_agent('computer_control', AGENT) == cs.TURNED_OFF
    assert _allowed(AGENT, 'computer_control') is False


# -- nothing changed, and the model is told why -------------------------------

@pytest.mark.parametrize('said', ['', 'everything', 'the copilot', 'my password',
                                  'scream', 'cameraman', None])
def test_an_input_that_names_no_permission_changes_nothing(said):
    _allow_all()
    assert cs.chat_off_type(said) is None
    reply = cs.turn_off_in_chat(said, AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was turned off: the input names no permission')
    assert 'camera, screen, computer control, data access' in reply
    assert 'ask them' in reply
    assert _rows(agent_id=AGENT) == [] and _allowed(AGENT) is True


def test_a_permission_that_is_not_on_says_so_and_writes_nothing():
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was changed: camera is not allowed for this agent')
    assert 'already off' in reply
    assert _rows() == [], 'a row was written for a permission nobody granted'


def test_turning_it_off_twice_is_told_as_already_off():
    _allow_all()
    assert cs.turn_off_for_agent('camera_capture', AGENT) == cs.TURNED_OFF
    assert cs.turn_off_for_agent('camera_capture', AGENT) == cs.NOT_ON
    assert len(_rows(agent_id=AGENT)) == 1


def test_someone_who_is_not_the_owner_changes_nothing():
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user='a-joined-parent')
    assert reply.startswith('Nothing was changed: only the owner of this computer')
    assert _allowed(AGENT) is True and _rows(agent_id=AGENT) == []


def test_a_turn_with_no_user_on_it_is_the_owners_as_for_the_ask():
    """ask_owner reads a missing turn user as "no turn check"; so does this."""
    _allow_all()
    assert cs.turn_off_for_agent('camera_capture', AGENT) == cs.TURNED_OFF


@pytest.mark.parametrize('prompt_id', [None, '', 0, '0'])
def test_a_turn_with_no_agent_changes_nothing(prompt_id):
    _allow_all()
    reply = cs.turn_off_in_chat('camera', prompt_id, turn_user=OWNER)
    assert reply.startswith('Nothing was changed: this chat is not with a named agent')
    assert 'Privacy settings' in reply
    assert _allowed(None) is True, 'allow-all was ended for everyone'
    assert [r for r in _rows() if r[3]] == [], 'something was revoked'


def test_with_no_owner_nothing_changes(monkeypatch):
    _allow_all()
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    reply = cs.turn_off_in_chat('camera', AGENT)
    assert reply.startswith('Nothing was changed: nobody is signed in')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    assert _allowed(AGENT) is True


def test_a_store_that_fails_is_told_and_never_raises(monkeypatch):
    _allow_all()
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: 1 / 0))
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was changed: the permission system is unavailable')
    assert 'Privacy settings' in reply


# -- the tool itself (source: the module cannot be imported here) -------------

def _hie():
    return _HIE.read_text(encoding='utf-8')


def test_the_tool_is_registered_beside_the_asks_with_its_own_function():
    src = _hie()
    m = re.search(r'labeled_tool\(\s*name="Turn_Off_Permission",\s*func=(\w+),', src)
    assert m, 'Turn_Off_Permission is not registered'
    assert m.group(1) == '_turn_off_consent'
    assert (src.index('name="Request_Screen_Access"')
            < src.index('name="Turn_Off_Permission"'))


def test_the_tool_answers_for_the_turns_own_agent_and_user():
    src = _hie()
    body = src[src.index('def _turn_off_consent('):]
    body = body[:body.index('\ndef ', 1)]
    assert 'turn_off_in_chat(input_text, thread_local_data.get_prompt_id()' in body
    assert 'turn_user=thread_local_data.get_user_id()' in body


def test_the_tool_has_its_progress_label():
    from core.constants import TOOL_LABELS
    assert TOOL_LABELS.get('Turn_Off_Permission')
