"""A person tells an agent in chat to stop using a permission, and it stops
for that agent only.

Owner, 2026-11-21: a consent an agent asked for once is kept "unless
explicitly asked to be turned off by user or user consents off in nunba
admin for that agent".  No agent tool could end a consent: Request_Camera_Access
and Request_Screen_Access only ask.  The Turn_Off_Permission tool
(hart_intelligence_entry._turn_off_consent) answers with
consent_service.turn_off_in_chat, whose write is ConsentService.decline for
the turn's own agent, the same no as the card's "Don't allow <agent>".

Review of f41434b5a (rv_59ff), fixed here:
- "data access" was offered and changed nothing: what reads it (the
  microphone and chat learning) asks for a person, never for an agent, so
  the agent's no was written and every reader still passed.  It is not
  offered; the switch never claims an effect it does not have.
- the write took a turn with no user on it, and an owner's goal turn, as the
  owner's.  It takes the owner's own turn only.

Owner, 2026-11-22: turning a permission back on takes back every agent's no
for it, and the switch says so before it is used.

Owner, 2026-11-22: "Real failure reasons should be available to the agent so
that it can ask correct questions".  Every refusal says its own reason: which
kind of turn it is, whether the permission was never allowed or already off,
and the store's own error with secrets masked.

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
    assert 'Tell the person' in reply and 'Permissions' in reply


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
    assert cs.turn_off_for_agent('computer_control', AGENT,
                                 turn_user=OWNER) == cs.TURNED_OFF
    assert _allowed(AGENT, 'computer_control') is False


# -- nothing changed, and the model is told why -------------------------------

@pytest.mark.parametrize('said', ['', 'everything', 'the copilot', 'my password',
                                  'scream', 'cameraman', None])
def test_an_input_that_names_no_permission_changes_nothing(said):
    _allow_all()
    assert cs.chat_off_type(said) is None
    reply = cs.turn_off_in_chat(said, AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was turned off: the input names no permission')
    assert 'one of: camera, screen, computer control.' in reply
    assert 'ask them' in reply
    assert _rows(agent_id=AGENT) == [] and _allowed(AGENT) is True


def test_a_permission_that_is_not_on_says_so_and_writes_nothing():
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was changed: camera is not allowed for this agent')
    assert 'already off' in reply
    assert _rows() == [], 'a row was written for a permission nobody granted'


def test_turning_it_off_twice_is_told_as_already_off():
    _allow_all()
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=OWNER) == cs.TURNED_OFF
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=OWNER) == cs.NOT_ON
    assert len(_rows(agent_id=AGENT)) == 1


def test_someone_who_is_not_the_owner_changes_nothing():
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user='a-joined-parent')
    assert reply.startswith('Nothing was changed: only the owner of this computer')
    assert 'in their own chat' in reply and 'Permissions' in reply, (
        f'the refusal does not say where the owner turns it off: {reply}')
    assert _allowed(AGENT) is True and _rows(agent_id=AGENT) == []


@pytest.mark.parametrize('turn_user', [None, '', '  '])
def test_a_turn_with_no_user_on_it_is_nobodys_and_writes_nothing(turn_user):
    """ask_owner reads a missing turn user as "no turn check": it only asks.
    This writes, so a turn that names nobody changes nothing."""
    _allow_all()
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=turn_user) == cs.NOT_THE_OWNER
    assert cs.turn_off_for_agent('camera_capture', AGENT) == cs.NOT_THE_OWNER
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=turn_user)
    assert reply.startswith('Nothing was changed: only the owner of this computer')
    assert _allowed(AGENT) is True and _rows(agent_id=AGENT) == []


@pytest.fixture
def goal_turn():
    """The turn is a daemon goal's: it runs under the owner's id with no
    person in it (dispatch.is_current_request_autonomous reads its id)."""
    from hartos.threadlocal import thread_local_data
    before = thread_local_data.get_request_id()
    thread_local_data.set_request_id('daemon_goal-123_1')
    yield
    thread_local_data.set_request_id(before)


def test_an_owners_goal_turn_turns_nothing_off(goal_turn):
    _allow_all()
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=OWNER) == 'background_turn'
    assert cs.BACKGROUND_TURN == 'background_turn'
    assert _allowed(AGENT) is True and _rows(agent_id=AGENT) == [], (
        'a background turn wrote a no in the owner\'s name')


def test_a_goal_turn_is_told_why_and_where_the_owner_does_it(goal_turn):
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was changed: this turn runs in the background')
    assert 'Do not call this tool again on this turn' in reply
    assert 'in their own chat' in reply and 'Permissions' in reply


def test_the_owners_own_turn_after_a_goal_turn_still_turns_it_off(goal_turn):
    from hartos.threadlocal import thread_local_data
    _allow_all()
    thread_local_data.set_request_id('req-a-persons-own')
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=OWNER) == cs.TURNED_OFF


def test_a_turn_that_cannot_be_read_changes_nothing(monkeypatch):
    import core.agent_tools as agent_tools
    _allow_all()
    monkeypatch.setattr(agent_tools, 'recall_runs_on_a_background_turn',
                        lambda: 1 / 0)
    assert cs.turn_off_for_agent('camera_capture', AGENT,
                                 turn_user=OWNER) == cs.ASK_FAILED
    assert _allowed(AGENT) is True and _rows(agent_id=AGENT) == []


# -- data access is not offered: no reader asks for an agent -------------------

@pytest.mark.parametrize('said', ['data access', 'Data Access', 'data_access',
                                  'stop using my data access'])
def test_data_access_is_not_offered_and_nothing_is_written(said):
    """Its readers (whisper_tool._mic_learning_consented, world_model_bridge
    ._ingest_user_utterance, lifecycle_hooks) check a person, not an agent:
    under allow-all they pass whatever one agent was told.  So the tool must
    not say "Done" for it."""
    _allow_all('data_access')
    assert cs.chat_off_type(said) is None
    reply = cs.turn_off_in_chat(said, AGENT, turn_user=OWNER)
    assert not reply.startswith('Done'), f'claimed an effect it does not have: {reply}'
    assert reply.startswith('Nothing was turned off')
    assert _rows(agent_id=AGENT) == [], 'a no nobody reads was written'
    with db_session() as db:
        assert ConsentService.check_consent(db, OWNER, 'data_access', scope='*') is True


def test_every_permission_offered_is_refused_for_the_agent_where_it_is_read():
    """What the switch may offer: a kind whose reader asks for its own
    agent.  After the turn-off the agent's own check fails and the reading
    the frame tools use says turned off; another agent's does not."""
    assert 'data_access' not in cs.CHAT_OFF_TYPES.values()
    for word, consent_type in cs.CHAT_OFF_TYPES.items():
        _allow_all(consent_type)
        cs.turn_off_in_chat(word, AGENT, turn_user=OWNER)
        assert _allowed(AGENT, consent_type) is False, consent_type
        assert cs.turned_off_for(consent_type, AGENT) is True, consent_type
        assert cs.turned_off_for(consent_type, OTHER) is False, consent_type


# -- turning it back on is for the whole kind, and that is said first ----------

def test_the_answer_says_turning_it_back_on_is_for_every_agent():
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert ('turning it back on turns it back on for every agent they turned '
            'it off for, not only this one') in reply, reply


def test_under_allow_all_the_way_back_named_is_the_one_that_exists():
    """Review of 2dd5600a9: the answer named "Privacy settings or Admin"
    whatever was on file.  Under "Allow ALL agents" the agent's row is in
    Admin with its switch, and that is what is named."""
    _allow_all()
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert 'they do that in Admin, under Agents, this agent, Permissions.' in reply
    assert 'Privacy settings' not in reply


def test_an_agent_that_held_its_own_grant_is_told_the_way_back_that_exists():
    """Its grant was its own: once ended nothing lists it in Admin and
    nothing turns it back on for that agent alone.  The only way back is to
    allow it for all agents, and the answer says that and nothing else."""
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'computer_control', agent_id=AGENT)
    reply = cs.turn_off_in_chat('computer control', AGENT, turn_user=OWNER)
    assert reply.startswith('Done: computer control is now turned off for this agent only')
    assert 'nothing turns it back on for this agent alone' in reply
    assert 'allow computer control for all their agents in Privacy settings' in reply
    assert 'Permissions' not in reply and 'every agent they turned it off' not in reply
    assert _allowed(AGENT, 'computer_control') is False
    # And that way back is real: allow-all given after the no covers it.
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'computer_control')
    assert _allowed(AGENT, 'computer_control') is True


def test_a_way_back_that_cannot_be_read_names_no_switch(monkeypatch):
    _allow_all()
    assert hasattr(cs, '_allow_all_stands'), 'the answer does not read what is on file'
    monkeypatch.setattr(ConsentService, 'active_grant',
                        staticmethod(lambda *a, **k: 1 / 0))
    assert cs._allow_all_stands('camera_capture') is None
    monkeypatch.undo()
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.setattr(cs, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_allow_all_stands', lambda consent_type: None)
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert reply.startswith('Done: camera is now turned off for this agent only')
    assert reply.endswith('they can allow it again in Privacy settings.')
    assert _allowed(AGENT) is False


def test_source_guard_the_tool_description_says_back_on_is_for_every_agent():
    """A check of source text, not of behaviour: the description is built
    inside get_tools, in a module that cannot be imported here.  What the
    tool answers is tested by behaviour above."""
    src = _hie()
    entry = src[src.index('name="Turn_Off_Permission"'):]
    entry = entry[:entry.index('ui_label=')]
    said = ' '.join(part.strip().strip('"') for part in entry.splitlines())
    said = ' '.join(said.split())
    assert 'back on for every agent it was turned off for' in said, said
    assert 'data access' not in said, 'the tool still offers data access'


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
    assert reply.startswith('Nothing was changed: camera could not be turned off '
                            'because the permission store failed')
    assert 'ZeroDivisionError: division by zero' in reply, (
        f'the store\'s own error is not told: {reply}')
    assert 'still on for this agent' in reply and 'Permissions' in reply
    assert 'unavailable' not in reply


def test_a_secret_in_the_stores_error_is_masked(monkeypatch):
    _allow_all()
    key = 'sk-proj-' + 'A1b2C3d4' * 5

    def boom(*a, **k):
        raise RuntimeError(f'could not reach the store with key {key} at 10.1.2.3')
    monkeypatch.setattr(ConsentService, 'check_consent', staticmethod(boom))
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert 'RuntimeError: could not reach the store' in reply, reply
    assert key not in reply, 'a secret reached the agent in a refusal'


def test_an_error_that_cannot_be_masked_is_told_by_its_kind_alone(monkeypatch):
    import security.edge_privacy as edge_privacy
    _allow_all()
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: 1 / 0))
    monkeypatch.setattr(edge_privacy, 'scrub_text', lambda text: 1 / 0)
    reply = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert 'permission store failed (ZeroDivisionError)' in reply, reply


def test_each_refusal_gives_its_own_reason():
    """No one sentence for two causes."""
    _allow_all()
    nobody = cs.turn_off_in_chat('camera', AGENT, turn_user=None)
    joiner = cs.turn_off_in_chat('camera', AGENT, turn_user='a-joined-parent')
    assert 'this turn names no person' in nobody and 'this turn names no person' not in joiner
    assert 'the person in this chat is not the owner' in joiner
    assert 'the person in this chat is not the owner' not in nobody
    # Never allowed, and already turned off, are two reasons.
    never = cs.turn_off_in_chat('screen', AGENT, turn_user=OWNER)
    assert 'nobody has allowed it yet' in never
    assert 'asks them the first time it needs it' in never
    cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    again = cs.turn_off_in_chat('camera', AGENT, turn_user=OWNER)
    assert 'the owner already turned it off for it' in again
    assert 'nobody has allowed it yet' not in again
    assert len({nobody, joiner, never, again}) == 4


def test_data_access_is_refused_with_why_it_cannot_be_done():
    _allow_all('data_access')
    reply = cs.turn_off_in_chat('stop using my data access', AGENT, turn_user=OWNER)
    assert reply.startswith('Nothing was turned off: data access cannot be turned off '
                            'for one agent')
    assert 'microphone and chat learning' in reply
    assert 'No page has a switch for it yet' in reply
    assert 'camera, screen, computer control' in reply
    assert 'names no permission' not in reply
    # Something that names nothing keeps its own reason.
    assert 'names no permission' in cs.turn_off_in_chat('everything', AGENT, turn_user=OWNER)


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
