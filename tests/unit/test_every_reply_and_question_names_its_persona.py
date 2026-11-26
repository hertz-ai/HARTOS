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

A question card's badge says which agent asks and whose session it asks
from: agent_name and agent_asker (the lookup that titles the card, the keys
consent cards carry), only on a card this node titles as the agent asking,
so a card a PERSON asks (the join card) has none; and persona, the ASKER's
persona, never the reader's (review of bdd6710ce).  to_persona is the
persona of the person the card is addressed to: the server does not know
which persona a device is, so a client compares it with its own.  What an
agent writes under any of these keys never reaches a card, through
person_ask.ask or through the shell's /api/a2ui ingest.  The push that
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
from unittest.mock import MagicMock, patch

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


def _goal(gid, owner_id):
    """A background goal whose person is ``owner_id``: a daemon turn asks
    its goal's person (person_ask.person_to_ask)."""
    from integrations.social.models import AgentGoal, db_session
    with db_session() as db:
        db.merge(AgentGoal(id=gid, goal_type='marketing', title='Practise words',
                           owner_id=owner_id, created_by=owner_id))


def _ask_as(session_user, component=None, request_id='req-1'):
    """An agent's question from ``session_user``'s session with the speech
    agent, through the ask_person tool the create and reuse loops build
    (core.agent_tools.build_core_tool_closures); ``request_id`` is the
    turn's.  Returns the ask's id."""
    from core.agent_tools import build_core_tool_closures
    user_prompt = f'{session_user}_{SPEECH}'
    ctx = {'user_id': session_user, 'prompt_id': SPEECH, 'agent_data': {},
           'helper_fun': MagicMock(), 'user_prompt': user_prompt,
           'request_id_list': {user_prompt: request_id}, 'recent_file_id': None,
           'scheduler': None, 'send_message_to_user1': lambda *a, **k: None,
           'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
           'save_conversation_db': lambda *a, **k: None}
    ask_person = {t[0]: t[2] for t in build_core_tool_closures(ctx)}['ask_person']
    reply = json.loads(ask_person(json.dumps(component or QUESTION), 0))
    assert reply.get('ask_id'), reply
    return reply['ask_id']


def _card(legs):
    _agent, card, _user = legs['cards'][-1]
    return card


def test_an_agents_question_names_the_askers_persona_and_whom_it_is_addressed_to(asks):
    """On a person's own turn the agent asks that person: the badge's persona
    is the asker's, their session's, and the card is addressed to them."""
    _ask_as(CHILD)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Child', 'Child')
    _ask_as(PARENT)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Parent', 'Parent')


def test_the_badge_carries_the_askers_persona_never_the_readers(asks):
    """Review of bdd6710ce (M2): a question asked from another person's
    session named the READER's persona on its badge.  A background goal of
    the child's session whose person is the parent asks the parent: the card
    is the parent's and is addressed to the parent, and its badge says the
    child's session asks."""
    _goal('g-words', PARENT)
    _ask_as(CHILD, request_id='daemon_g-words')
    _agent, card, to = asks['cards'][-1]
    assert to == PARENT
    assert (card['persona'], card['to_persona']) == ('Child', 'Parent')


def test_a_card_a_person_asks_has_no_agent_badge(asks):
    """Review of bdd6710ce (M2): the join card says a PERSON asks
    (reuse_recipe._reuse_join_card titles it so; review M2 of 11dbbd8ef), and
    an agent badge above it read as the agent asking.  A card with a title of
    its own gets no agent name, as before bdd6710ce, and names no asker."""
    from integrations.agent_engine import person_ask
    join = {'type': 'approval', 'action': f'join_chat:{SPEECH}:{PARENT}',
            'title': f'Meena (user {PARENT}) asks to join your chat',
            'description': (f'Meena (user {PARENT}) asks to join your chat with Speech '
                            f'Buddy (agent {SPEECH}) as Parent. Allow?')}
    assert person_ask.ask(CHILD, SPEECH, join, prompt_id=SPEECH)['ok'] is True
    card = _card(asks)
    assert card['title'] == f'Meena (user {PARENT}) asks to join your chat'
    assert not {'agent_name', 'agent_asker', 'persona'} & set(card), card
    assert card['to_persona'] == 'Child'


def test_the_card_names_the_asking_agent_for_its_badge(asks):
    """The page's badge reads agent_name / agent_asker (Nunba AgentOverlay,
    askerOf), which a question card did not carry, so a question showed no
    badge at all.  Both come from the lookup that titles the card."""
    _ask_as(CHILD)
    card = _card(asks)
    assert card['title'] == 'Agent Speech Buddy asks you'
    assert (card['agent_name'], card['agent_asker']) == ('Speech Buddy', 'Agent Speech Buddy')


def test_the_phone_reads_the_same_card(asks):
    """A tapped banner opens the app, and the phone reads the card from this
    node (read_ask): it says the same."""
    from integrations.agent_engine import person_ask
    ask_id = _ask_as(CHILD)
    card, status = person_ask.read_ask(ask_id, CHILD)
    assert status == 200
    assert {k: card.get(k) for k in ('persona', 'to_persona', 'agent_name', 'agent_asker')} == {
        'persona': 'Child', 'to_persona': 'Child', 'agent_name': 'Speech Buddy',
        'agent_asker': 'Agent Speech Buddy'}


def test_the_push_that_leaves_the_node_carries_no_persona(asks):
    """Card only (design_1125 B2): the push goes to Google or central's
    relay, and the phone reads the card anyway."""
    _ask_as(CHILD)
    _settle()
    assert _card(asks)['persona'] == 'Child'
    [(_uid, _title, _body, data)] = asks['pushes']
    assert not PERSONA_KEYS & set(data), data


def test_with_no_persona_placed_the_card_has_no_persona_keys(asks):
    _ask_as(CHILD)
    assert _card(asks)['persona'] == 'Child'
    _ask_as(NEWCOMER)
    card = _card(asks)
    assert not PERSONA_KEYS & set(card), card


def test_what_the_agent_writes_in_its_component_never_names_the_persona_or_the_asker(asks):
    """An agent writes the component; who asks and for whom are the
    server's to say.  A model's 'persona' is a guess, not a persona placed."""
    spoofed = dict(QUESTION, persona='Therapist', to_persona='Therapist',
                   agent_name='Your Bank', agent_asker='Your Bank')
    _ask_as(CHILD, spoofed)
    card = _card(asks)
    assert (card['persona'], card['to_persona']) == ('Child', 'Child')
    assert (card['agent_name'], card['agent_asker']) == ('Speech Buddy', 'Agent Speech Buddy')
    _ask_as(NEWCOMER, spoofed)
    assert not PERSONA_KEYS & set(_card(asks)), _card(asks)


def test_what_an_agent_pushes_to_the_shell_never_names_who_asks_or_for_whom(monkeypatch):
    """Review of bdd6710ce (nit): the badge keys were dropped only inside
    person_ask.ask.  The shell's /api/a2ui ingest takes an agent's component
    as the agent wrote it, so the page's badge would show the agent's own
    'Your Bank'.  Only this node sets those keys."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    import integrations.social.realtime as rt
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    svc = LiquidUIService(a2ui_enabled=True)
    app = svc._create_flask_app()
    app.config['TESTING'] = True
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted', return_value=False):
        r = app.test_client().post('/api/a2ui', json={'agent_id': SPEECH, 'component': {
            'type': 'card', 'title': 'Please confirm your account',
            'agent_name': 'Your Bank', 'agent_asker': 'Your Bank',
            'persona': 'Therapist', 'to_persona': 'Therapist'}})
    assert r.status_code == 200 and r.get_json()['success'] is True
    shown = svc._agent_components[SPEECH][-1]
    assert shown['title'] == 'Please confirm your account'
    assert not ({'agent_name', 'agent_asker'} | PERSONA_KEYS) & set(shown), shown


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
    """A reply whose caller passes no prompt_id (many callers do pass one):
    the /chat body names the agent the turn was for, as the chat mirror reads
    it (a number, as the page sends it)."""
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
