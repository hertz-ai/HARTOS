"""In the persona chat a tool's note to the model is never the person's reply,
and the join tool joins only an account the person named in their own words.

Live walk, 2026-11-22 14:17 IST, the Tamil speech agent, a fresh account that
typed "Parent":
- the model called update_persona(new=False, contact_number=
  'user_id_placeholder'): an id nobody said.  contact_number was a required
  argument, so even a person starting their own chat made the model put
  something there;
- the persona chat ended on the tool's result (its round limit), and the
  turn answered the group's last message whatever it was, so the person was
  read "There is no account user_id_placeholder on this computer, so no one
  was asked and nothing was changed. ... never make one up", twice.

The real role group (reuse_recipe.create_agents_for_role), its real
update_persona tool and the real join; the model is scripted, the consent
ask is a recorded fake, and the data dir is a temp folder.

    python -m pytest tests/unit/test_a_tool_note_is_never_the_persons_reply.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('CLAUDE_CONFIG_DIR', tempfile.mkdtemp(prefix='persona_chat_test_'))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

PARENT, P = 'u-parent-walk', 424243
PERSONAS = ['Child', 'Parent', 'Therapist']
NOTE_HEADS = ('There is no account', 'is not something the person said',
              'No account was named', 'Ask the person for the user id')


class _History:
    def __init__(self):
        self.messages = []

    def add_message(self, msg, metadata=None):
        self.messages.append(msg)

    def search_by_metadata(self, **_kw):
        return []


class Asks:
    def __init__(self):
        self.sent = []

    def ask(self, user_id, agent_id, component, *, prompt_id=None, **_k):
        self.sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(self.sent)}', 'refused': None, 'card': True}


@pytest.fixture
def chat(monkeypatch, tmp_path):
    """The real persona chat of a three-persona agent for PARENT."""
    import core.event_attribution as event_attribution
    import core.platform_paths as platform_paths
    import integrations.channels.memory.shared_history as sh
    from hartos import reuse_recipe as rr
    from integrations.agent_engine import person_ask
    from integrations.social.models import Base, get_engine
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [
        {'name': n, 'description': f'the {n.lower()}'} for n in PERSONAS], 'flows': []}),
        encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    monkeypatch.setattr(rr, 'get_coding_workspace_dir', lambda: str(tmp_path))
    monkeypatch.setattr(sh, '_get_persistent_history', lambda user_id: _History())
    asks = Asks()
    monkeypatch.setattr(person_ask, 'ask', asks.ask)
    monkeypatch.setattr(event_attribution, 'person_for', lambda uid, *_a, **_k: str(uid))
    Base.metadata.create_all(get_engine())
    with Flask('persona-chat-test').app_context():
        agents = rr.create_agents_for_role(PARENT, P)
        assert agents[-1] is False, 'three personas must build the persona chat'
        yield _Chat(rr, agents, asks)


class _Chat:
    def __init__(self, rr, agents, asks):
        self.rr, self.asks = rr, asks
        (self.assistant, self.user_proxy, self.group, self.manager,
         self.helper, _stop) = agents
        self.script = []
        self.calls = 0
        import autogen
        self.assistant.register_reply([autogen.Agent, None], self._model, position=0)

    def _model(self, recipient, messages=None, sender=None, config=None):
        """The scripted model: the next line of the script, each time the
        assistant is asked to speak."""
        self.calls += 1
        assert self.script, 'the assistant was asked to speak more often than scripted'
        return True, self.script.pop(0)

    def turn(self, text, *script):
        """One turn of the persona chat, as chat_agent runs it; returns the
        reply the turn gives the person."""
        self.script = list(script)
        start = len(self.group.messages)
        self.user_proxy.initiate_chat(self.manager, message=text,
                                      speaker_selection={'speaker': 'assistant'},
                                      clear_history=False)
        last = self.group.messages[-1]
        if 'terminate' in str(last.get('content') or '').lower():
            return 'TERMINATE'
        pick = getattr(self.rr, '_reuse_role_reply', None)
        if pick is None:        # how the turn picked before there was a rule
            return last['content']
        return pick(self.group.messages, start, P)

    def notes(self):
        return [m['content'] for m in self.group.messages
                if m.get('role') == 'tool' or m.get('tool_responses')]


def _call(**arguments):
    """The model's update_persona call, as autogen carries it."""
    return {'content': None, 'role': 'assistant', 'tool_calls': [{
        'id': f'call_{uuid.uuid4().hex[:8]}', 'type': 'function',
        'function': {'name': 'update_persona', 'arguments': json.dumps(arguments)}}]}


def _account():
    from integrations.social.models import User, db_session
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'child_{uid[:8]}', display_name='Arun',
                    user_type='human', api_token=f'tok-{uid}'))
    return uid


def _is_a_note(reply):
    return any(head in str(reply) for head in NOTE_HEADS)


# -- the walk, as it happened ---------------------------------------------------

def test_the_walk_a_made_up_id_is_never_read_to_the_parent(chat):
    """"Parent" -> the model joins with an id nobody said.  The person must
    get the assistant's words, not the tool's note."""
    reply = chat.turn(
        'Parent',
        _call(name='Parent', description="child's mother", new=False,
              contact_number='user_id_placeholder'),
        'Would you like to join your child\'s chat, or start your own as Parent?')
    assert not _is_a_note(reply), f'the tool\'s note to the model was the reply: {reply!r}'
    assert 'user_id_placeholder' not in reply
    assert reply == 'Would you like to join your child\'s chat, or start your own as Parent?'
    assert chat.asks.sent == [], 'someone was asked on a made-up id'


def test_the_note_still_reaches_the_model_with_the_real_reason(chat):
    chat.turn('Parent',
              _call(name='Parent', description='x', new=False,
                    contact_number='user_id_placeholder'),
              'Do you want to join someone, or start your own chat?')
    (note,) = chat.notes()
    assert "'user_id_placeholder' is not something the person said in this chat" in note, note
    assert 'no one was asked and nothing was changed' in note
    assert "ask them for that person's user id" in note
    assert 'call update_persona with new=True' in note
    assert chat.calls == 2, 'the assistant never read the note'


def test_the_model_can_put_its_call_right_in_the_same_turn(chat):
    """After the note the assistant gets a round: the parent who only named
    their persona is set up in one turn."""
    reply = chat.turn(
        'Parent',
        _call(name='Parent', description='x', new=False,
              contact_number='user_id_placeholder'),
        _call(name='Parent', description='x', new=True))
    assert reply == 'TERMINATE', reply
    assert chat.rr.agents_roles[f'{PARENT}_{P}'][PARENT] == 'Parent'
    from core.flow_progress import stored_persona
    assert stored_persona(PARENT, P, PERSONAS) == 'Parent'


def test_a_model_that_only_calls_tools_leaves_the_person_the_persona_question(chat):
    reply = chat.turn(
        'Parent',
        _call(name='Parent', description='x', new=False, contact_number='placeholder'),
        _call(name='Parent', description='x', new=False, contact_number='12345'))
    assert not _is_a_note(reply), reply
    assert reply == 'Which of these are you: Child, Parent, Therapist?'
    assert len(chat.notes()) == 2, 'the notes are kept for the model'


# -- starting one's own chat needs no id ------------------------------------------

def test_starting_ones_own_chat_takes_no_contact_number(chat):
    tool = next(t['function'] for t in chat.assistant.llm_config['tools']
                if t['function']['name'] == 'update_persona')
    assert 'contact_number' in tool['parameters']['properties'], 'the argument is gone'
    assert 'contact_number' not in tool['parameters'].get('required', []), (
        'the model has to put something there even for a person starting their own chat')
    assert {'name', 'description', 'new'} <= set(tool['parameters']['required'])
    assert chat.turn('I am the parent',
                     _call(name='Parent', description='x', new=True)) == 'TERMINATE'
    assert chat.rr.agents_roles[f'{PARENT}_{P}'][PARENT] == 'Parent'


def test_a_join_with_no_account_named_is_told_both_ways_on(chat):
    reply = chat.turn('Parent', _call(name='Parent', description='x', new=False),
                      'Whose chat would you like to join?')
    assert reply == 'Whose chat would you like to join?'
    (note,) = chat.notes()
    assert note.startswith('No account was named, so no one was asked')
    assert 'call update_persona with new=True' in note


# -- an account the person did name -------------------------------------------------

def test_an_account_the_person_named_is_asked_as_before(chat):
    child = _account()
    reply = chat.turn(f'I am the parent, I want to join the chat of {child}',
                      _call(name='Parent', description='x', new=False,
                            contact_number=child),
                      'I have asked them. Message me again once they allow it.')
    assert [who for who, _card in chat.asks.sent] == [child]
    assert reply == 'I have asked them. Message me again once they allow it.'
    (note,) = chat.notes()
    assert note.startswith(f'I have asked the account holder of {child}')


def test_an_id_given_in_an_earlier_turn_still_counts(chat):
    child = _account()
    chat.turn(f'My child is {child.upper()}.', 'And which persona are you?')
    chat.turn('Parent', _call(name='Parent', description='x', new=False,
                              contact_number=child),
              'I have asked them.')
    assert [who for who, _card in chat.asks.sent] == [child]


def test_part_of_what_the_person_said_is_not_the_id_they_gave(chat):
    child = _account()
    chat.turn(f'Join me to {child}',
              _call(name='Parent', description='x', new=False,
                    contact_number=child[:8]),
              'Could you give me the whole id?')
    assert chat.asks.sent == []
    assert 'is not something the person said' in chat.notes()[0]


def test_what_the_agent_or_a_tool_said_is_not_the_persons_word(chat):
    """An id that only the assistant or a tool's note carried was never said
    by the person."""
    child = _account()
    chat.turn('Parent', f'Is your child {child}?')
    chat.turn('yes', _call(name='Parent', description='x', new=False,
                           contact_number=child),
              'Please type the id yourself.')
    assert chat.asks.sent == [], 'an id the person never typed was joined'


def test_a_line_seeded_from_the_history_is_not_the_persons_word(chat):
    """The history write-back files the assistant's words as the person's,
    so a seeded 'User' line ("Is your child <id>?") is not the person's."""
    child = str(uuid.uuid4())
    seeded = [{'role': 'user', 'name': 'User', 'content': f'Is your child {child}?',
               '_from_shared': True},
              {'role': 'assistant', 'name': 'assistant', 'content': f'Joining {child}.',
               '_from_shared': True}]
    assert hasattr(chat.rr, '_reuse_persons_words'), "nothing reads the person's own words"
    words = chat.rr._reuse_persons_words(
        seeded + [{'name': 'user', 'role': 'user', 'content': 'Parent'}], chat.user_proxy.name)
    assert child not in words
    assert words == 'Parent'


# -- the pick, shape by shape: nothing that worked gets worse ------------------------

def _pick(rr, messages, start=0):
    assert hasattr(rr, '_reuse_role_reply'), 'the persona chat has no rule for its reply'
    with Flask('pick-test').app_context():
        return rr._reuse_role_reply(messages, start, P)


def test_the_assistants_question_is_the_reply_as_before(chat):
    reply = chat.turn('hello', 'Which persona are you: Child, Parent or Therapist?')
    assert reply == 'Which persona are you: Child, Parent or Therapist?'
    assert chat.calls == 1, 'a plain question cost a second model call'


def test_only_this_turns_words_are_picked(chat):
    rr = chat.rr
    earlier = [{'name': 'user', 'role': 'user', 'content': 'hello'},
               {'name': 'assistant', 'role': 'user', 'content': 'An old question?'}]
    now = [{'name': 'user', 'role': 'user', 'content': 'Parent'},
           {'name': 'assistant', 'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c', 'type': 'function',
                            'function': {'name': 'update_persona', 'arguments': '{}'}}]},
           {'name': 'Helper', 'role': 'tool', 'content': 'There is no account x',
            'tool_responses': [{'role': 'tool', 'tool_call_id': 'c',
                                'content': 'There is no account x'}]}]
    assert _pick(rr, earlier + now, start=2) == 'Which of these are you: Child, Parent, Therapist?'
    spoke = now + [{'name': 'assistant', 'role': 'user', 'content': 'Whose chat?'}]
    assert _pick(rr, earlier + spoke, start=2) == 'Whose chat?'
    # Words before a later tool result of the same turn are still the agent's.
    assert _pick(rr, earlier + spoke + now[1:], start=2) == 'Whose chat?'
    # A log shorter than its start reads its end, as before.
    assert _pick(rr, earlier, start=9) == 'An old question?'


def test_the_older_function_style_result_is_a_note_too(chat):
    log = [{'name': 'user', 'role': 'user', 'content': 'Parent'},
           {'name': 'Helper', 'role': 'function', 'content': 'There is no account x'}]
    assert _pick(chat.rr, log) == 'Which of these are you: Child, Parent, Therapist?'


def test_source_guard_the_turn_answers_through_the_rule():
    """A check of source text: the persona branch of chat_agent cannot be run
    here.  Its reply is _reuse_role_reply's, over the turn's own messages."""
    from pathlib import Path
    src = (Path(ROOT) / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
    branch = src[src.index("if user_journey[user_prompt] == 'Roles':"):]
    branch = branch[:branch.index('create_schedule(prompt_id, user_id)')]
    assert "return last_message['content']" not in branch
    assert 'return _reuse_role_reply(group_chat.messages, _role_turn_start,' in branch
    assert branch.index('_role_turn_start = len(group_chat.messages)') < branch.index(
        'user_proxy.initiate_chat(')
