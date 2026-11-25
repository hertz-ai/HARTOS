"""Every message, question and /chat reply names the persona it is for.

Owner, 2026-11-25: "my computer desktop is child personna, where are we
showing the persona with agent name to user in chat screen?"  Design note
design_1125, section B (reviewed by hartos-17 and an independent reviewer,
v3).  The server keeps one persona per account and agent (core.flow_progress
'persona', written only by set_persona, from update_persona and an approved
join), and before this change nothing that reaches a client carried it: not
the pushed agent message (core.peer_link.crossbar_publish
.publish_agent_message), not the question card (integrations.agent_engine
.person_ask.ask), not the /chat reply (hart_intelligence_entry._chat_reply).
So no screen could say "<agent> · <persona>".

One reader, core.flow_progress.persona_of, for all three: the person's kept
pick while it is one of the agent's personas, else the agent's only persona.
No persona placed means no key: no guess (CREATE's in-memory member list
holds every persona under the creator), never 'user', never ''.

The card also says whom it is addressed to (to_persona: the server does not
know which persona a device is, so the client compares it with its own) and
names the asking agent for the page's badge (agent_name, agent_asker: the
lookup the card's title already uses, the keys consent cards carry).  What an
agent writes into its component never sets any of these.  The push that
leaves the node carries no persona key: the phone reads the card (read_ask).

Real flow_progress store and agent prompt files on temp dirs; real
person_ask with NotificationService and the in-memory social DB (the
``legs`` fixture of test_person_ask records the card, the push and the
events); the real publish_agent_message with its publisher recorded; the
real _chat_reply, its source run in an isolated namespace as
test_consent_fanout_p2 runs it, with TTS, memory and the chat mirror
recorded.

Written red: on the tree of 5ecf725e8 all 16 failed, each for the missing
behaviour (no persona_of; no persona on the pushed message, the card or the
/chat reply; no agent name on the card; the component's 'Therapist' kept).

    python -m pytest tests/unit/test_every_reply_and_question_names_its_persona.py -q
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_person_ask import _no_open_asks, _settle, legs  # noqa: E402,F401

#: The demo agent (design_1125: 95568353503) and its personas; the accounts
#: of the child, the parent who joined, and a newcomer who has not picked.
SPEECH = '95568353503'
PERSONAS = ['Child', 'Parent', 'Therapist']
#: An agent with one persona, and an id no agent has.
SOLO, NO_AGENT = '70000000001', '99999999999'
CHILD, PARENT, NEWCOMER = '10202', '777', '10203'
QUESTION = {'type': 'form',
            'description': 'So the lesson calls the child by the name they answer to.',
            'fields': [{'name': 'home_name', 'type': 'text',
                        'label': 'What does the family call the child at home?'}]}
PERSONA_KEYS = {'persona', 'to_persona'}
HIE = os.path.join(ROOT, 'hart_intelligence_entry.py')


def _agent_file(prompts, prompt_id, name, personas):
    path = prompts / f'{prompt_id}.json'
    path.write_text(json.dumps({
        'name': name, 'prompt_id': prompt_id,
        'personas': [{'name': p, 'description': f'the {p.lower()}'} for p in personas],
        'flows': [{'flow_name': f'{p} flow', 'persona': p, 'actions': ['greet'],
                   'sub_goal': ''} for p in personas]}), encoding='utf-8')
    return path


@pytest.fixture
def kept(tmp_path, monkeypatch):
    """The agents' prompt files and the picks kept on this node, on temp
    dirs: the speech agent with three personas, where the child and the
    parent have picked and the newcomer has not, and an agent with one
    persona."""
    import core.cache_loaders as cache_loaders
    import core.flow_progress as fp
    import core.platform_paths as platform_paths
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path / 'data'))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    _agent_file(prompts, SPEECH, 'Speech Buddy', PERSONAS)
    _agent_file(prompts, SOLO, 'Word Coach', ['Learner'])
    # Picked as the person said it, kept as the agent spells it.
    assert fp.set_persona(CHILD, SPEECH, ' child ', PERSONAS) == 'Child'
    assert fp.set_persona(PARENT, SPEECH, 'Parent', PERSONAS) == 'Parent'
    return SimpleNamespace(fp=fp, prompts=prompts)


# -- the one reader ------------------------------------------------------------

def test_the_persona_is_the_persons_kept_pick_as_the_agent_spells_it(kept):
    assert kept.fp.persona_of(CHILD, SPEECH) == 'Child'
    assert kept.fp.persona_of(PARENT, SPEECH) == 'Parent'


def test_an_agent_with_one_persona_names_it_for_everyone(kept):
    """A one-persona agent never asks the persona question, and its runs use
    that persona (flow_progress.place_persona): it is placed."""
    assert kept.fp.persona_of(NEWCOMER, SOLO) == 'Learner'


def test_no_pick_among_several_personas_is_no_persona_never_a_guess(kept):
    """CREATE registers the agent's WHOLE persona list under the creator
    (persona_registry.register_persona_for_session), and reuse_recipe
    .get_role reads its first entry for someone who never picked: that is a
    guess, and no screen shows it."""
    from core.persona_registry import (
        agents_roles, agents_session, register_persona_for_session)
    key = f'{NEWCOMER}_{SPEECH}'
    register_persona_for_session(NEWCOMER, SPEECH, [{'name': p} for p in PERSONAS])
    try:
        assert kept.fp.persona_of(NEWCOMER, SPEECH) is None
        assert kept.fp.persona_of(CHILD, SPEECH) == 'Child'
    finally:
        agents_session.pop(key, None)
        agents_roles.pop(key, None)


def test_a_pick_the_agent_no_longer_has_is_no_persona(kept):
    path = _agent_file(kept.prompts, SPEECH, 'Speech Buddy', ['Child', 'Therapist'])
    later = time.time() + 5
    os.utime(path, (later, later))          # the config cache keys on mtime
    assert kept.fp.persona_of(PARENT, SPEECH) is None
    assert kept.fp.persona_of(CHILD, SPEECH) == 'Child'


def test_no_agent_record_or_no_person_is_no_persona(kept):
    assert kept.fp.persona_of(CHILD, NO_AGENT) is None
    assert kept.fp.persona_of(CHILD, None) is None
    assert kept.fp.persona_of(None, SOLO) is None
    assert kept.fp.persona_of('', SOLO) is None


# -- the pushed message --------------------------------------------------------

@pytest.fixture
def publisher(monkeypatch):
    """The hart_intelligence publish_async, as safe_hartos_attr resolves it."""
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


def _pushed(pub):
    assert pub.call_count == 1, pub.call_args_list
    topic, payload = pub.call_args[0][0], pub.call_args[0][1]
    return topic, (json.loads(payload) if isinstance(payload, str) else payload)


def _send(person, agent=SPEECH, text='Say "ball" with me.'):
    from core.peer_link.crossbar_publish import publish_agent_message
    assert publish_agent_message(text=text, user_id=person,
                                 request_id='rid-1-intermediate', prompt_id=agent)


def test_a_pushed_message_names_the_persona_of_the_person_it_is_for(kept, publisher):
    for person, persona in ((CHILD, 'Child'), (PARENT, 'Parent')):
        publisher.reset_mock()
        _send(person)
        topic, payload = _pushed(publisher)
        assert topic == f'com.hertzai.hevolve.chat.{person}'
        assert payload['persona'] == persona


def test_a_pushed_message_with_no_persona_placed_has_no_persona_key(kept, publisher):
    _send(CHILD)
    assert _pushed(publisher)[1]['persona'] == 'Child'
    for person, agent in ((NEWCOMER, SPEECH), (CHILD, NO_AGENT)):
        publisher.reset_mock()
        _send(person, agent)
        assert 'persona' not in _pushed(publisher)[1]


# -- the question card ---------------------------------------------------------

def _agent_named(agent_id, name):
    """The agent's social mirror, the first place its name is looked up
    (consent_service.agent_display_name)."""
    from integrations.social.models import User, db_session
    with db_session() as db:
        if not db.query(User).filter_by(agent_id=agent_id).first():
            db.add(User(id='agent-' + agent_id, username='speech.local.' + agent_id,
                        display_name=name, user_type='agent', agent_id=agent_id,
                        api_token='tok-' + agent_id))


@pytest.fixture
def asks(kept, legs):
    _agent_named(SPEECH, 'Speech Buddy')
    return legs


def _ask(person, component=None):
    from integrations.agent_engine import person_ask
    out = person_ask.ask(person, SPEECH, dict(component or QUESTION), prompt_id=SPEECH)
    assert out['ok'] is True, out
    return out['ask_id']


def _card(legs):
    _agent, card, _user = legs['cards'][-1]
    return card


def test_the_card_names_the_persona_it_is_for_and_whom_it_is_addressed_to(asks):
    _ask(CHILD)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Child', 'Child')
    _ask(PARENT)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Parent', 'Parent')


def test_the_card_names_the_asking_agent_for_its_badge(asks):
    """The page's badge reads agent_name / agent_asker (Nunba AgentOverlay,
    askerOf), which a question card did not carry, so a question showed no
    badge at all.  Both come from the lookup that titles the card."""
    _ask(CHILD)
    card = _card(asks)
    assert card['title'] == 'Agent Speech Buddy asks you'
    assert (card['agent_name'], card['agent_asker']) == ('Speech Buddy', 'Agent Speech Buddy')


def test_the_phone_reads_the_same_card(asks):
    """A tapped banner opens the app, and the phone reads the card from this
    node (read_ask): it says the same."""
    from integrations.agent_engine import person_ask
    ask_id = _ask(CHILD)
    card, status = person_ask.read_ask(ask_id, CHILD)
    assert status == 200
    assert {k: card.get(k) for k in ('persona', 'to_persona', 'agent_name', 'agent_asker')} == {
        'persona': 'Child', 'to_persona': 'Child', 'agent_name': 'Speech Buddy',
        'agent_asker': 'Agent Speech Buddy'}


def test_the_push_that_leaves_the_node_carries_no_persona(asks):
    """Card only (design_1125 B2): the push goes to Google or central's
    relay, and the phone reads the card anyway."""
    _ask(CHILD)
    _settle()
    assert _card(asks)['persona'] == 'Child'
    [(_uid, _title, _body, data)] = asks['pushes']
    assert not PERSONA_KEYS & set(data), data


def test_with_no_persona_placed_the_card_has_no_persona_keys(asks):
    _ask(CHILD)
    assert _card(asks)['persona'] == 'Child'
    _ask(NEWCOMER)
    card = _card(asks)
    assert not PERSONA_KEYS & set(card), card


def test_what_the_agent_writes_in_its_component_never_names_the_persona_or_the_asker(asks):
    """An agent writes the component; who asks and for whom are the
    server's to say.  A model's 'persona' is a guess, not a persona placed."""
    spoofed = dict(QUESTION, persona='Therapist', to_persona='Therapist',
                   agent_name='Your Bank', agent_asker='Your Bank')
    _ask(CHILD, spoofed)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Child', 'Child')
    assert (card['agent_name'], card['agent_asker']) == ('Speech Buddy', 'Agent Speech Buddy')
    _ask(NEWCOMER, spoofed)
    assert not PERSONA_KEYS & set(_card(asks)), _card(asks)


# -- the /chat reply -----------------------------------------------------------

@pytest.fixture
def chat_reply(kept, monkeypatch):
    """The real _chat_reply, its source run in an isolated namespace
    (importing hart_intelligence_entry takes minutes), with TTS, memory and
    the chat mirror recorded and jsonify giving back the reply's dict."""
    from integrations.social import chat_messages
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', MagicMock())
    with open(HIE, encoding='utf-8') as f:
        src = f.read()
    found = re.search(r'(?ms)^def _chat_reply\([\s\S]+?'
                      r'(?=^def [a-zA-Z_]|^class [a-zA-Z_]|^@[a-zA-Z_]|\Z)', src)
    assert found, '_chat_reply is not in hart_intelligence_entry.py'
    ns = {'__name__': 'isolated_chat_reply', 'app': MagicMock(),
          'jsonify': lambda payload: payload, 'logging': logging, 'threading': threading,
          '_tts_synthesize_and_publish': MagicMock(), 'get_memory': lambda **_k: None,
          '_get_or_create_graph': lambda *_a, **_k: None}
    exec(compile(textwrap.dedent(found.group(0)), '<isolated:_chat_reply>', 'exec'), ns)
    return ns['_chat_reply']


def test_the_chat_reply_names_the_persona_of_the_turns_person(chat_reply):
    reply = chat_reply(CHILD, 'rid-1', 'Well done!', prompt_id=SPEECH, user_prompt='ball')
    assert reply['response'] == 'Well done!'
    assert reply['persona'] == 'Child'
    assert chat_reply(PARENT, 'rid-2', 'Thank you.', prompt_id=SPEECH)['persona'] == 'Parent'


def test_the_chat_reply_takes_the_agent_from_the_turns_body(chat_reply):
    """No caller passes prompt_id: the /chat body names the agent the turn
    was for, as the chat mirror reads it (a number, as the page sends it)."""
    from flask import Flask
    with Flask('persona_test').test_request_context(
            '/chat', json={'prompt': 'ball', 'prompt_id': int(SPEECH), 'request_id': 'rid-3'}):
        reply = chat_reply(CHILD, 'rid-3', 'Well done!', user_prompt='ball')
    assert reply['persona'] == 'Child'


def test_a_chat_reply_with_no_persona_placed_has_no_persona_key(chat_reply):
    assert chat_reply(CHILD, 'rid-4', 'Hi!', prompt_id=SPEECH)['persona'] == 'Child'
    assert 'persona' not in chat_reply(NEWCOMER, 'rid-5', 'Hi!', prompt_id=SPEECH)
    assert 'persona' not in chat_reply(CHILD, 'rid-6', 'Hi!', prompt_id=NO_AGENT)
    assert 'persona' not in chat_reply(CHILD, 'rid-7', 'Hi!')
