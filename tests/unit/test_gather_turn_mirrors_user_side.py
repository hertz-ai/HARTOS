"""A gather-requirements turn must record the USER's side, not only the reply.

MEASURED LIVE (livetest gather_requirements GR7, Nunba HARTOS 20bfca03d):
POST :5000/chat {user_id:'livetest_gather_requirements_verify',
prompt_id:'7700000091', prompt:'I want an agent that summarises my daily
news', create_agent:true}.  The reply left through the gather phase, and a
read-only query of conversation_entries for that user returned ONLY the
assistant row.

ROOT CAUSE, read in full: `_chat_reply` is the ONE place every /chat reply
goes through, and it writes the user row to the conversation mirror
(chat_messages.persist_and_publish_async), the SimpleMem save_context and the
MemoryGraph register ONLY when the caller passes `user_prompt`.  The five
gather-phase returns in chat() (pending question, empty-build re-ask,
completed, salvage, parse-error) never passed it, so every creation
conversation was stored as an assistant monologue: the other device's mirror
showed questions with no answers, and memory never held what the user said.

WHAT IS RECORDED: the words the user sent this turn -- NOT the text handed to
gather_info, which on an agent's first turn is augmented with the cloud
record (" name:... goal:...") and on the last turn is replaced by the
forced-completion instruction.  Both are asserted below.

WHY THE HANDLER IS EXECUTED FROM SOURCE: no interpreter on the dev box imports
hart_intelligence_entry (langchain_classic is absent from the test venv).  So
the REAL chat(), _chat_reply() and _config_is_buildable() are lifted out of the
real file with `ast` and run in one namespace against a real Flask request;
only the boundaries (LLM gather_info, DB/network, chat_messages writer, memory)
are stubbed.  HARTOS_ENTRY_SOURCE points it at another copy of the file (used
for the mutation run).

    python -m pytest tests/unit/test_gather_turn_mirrors_user_side.py --noconftest -q
"""
import ast
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

flask = pytest.importorskip('flask')

from core.constants import canonical_served_by as _canonical_served_by  # noqa: E402

_SRC = os.environ.get('HARTOS_ENTRY_SOURCE') or str(
    Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py')

USER_TEXT = 'I want an agent that summarises my daily news'
USER_ID = 'livetest_gather_requirements_unit'
PROMPT_ID = '7700000091'


def _lift(names, optional=()):
    """Source of the named top-level defs / assignments, in file order.

    ``optional`` names are lifted when the file has them: helpers chat()
    calls only in later revisions, so the same test runs on an older copy
    (HARTOS_ENTRY_SOURCE) and fails there on behaviour, not on a name."""
    src = open(_SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    wanted = set(names) | set(optional)
    out, found = [], set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            out.append(ast.get_source_segment(src, node))
            found.add(node.name)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in wanted):
            out.append(ast.get_source_segment(src, node))
            found.add(node.targets[0].id)
    missing = set(names) - found
    assert not missing, (
        f'{sorted(missing)} not found in {_SRC} -- re-point this test '
        f'rather than deleting it')
    return '\n\n'.join(out)


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        if self._t:
            self._t(*self._a, **self._k)


class _Inline:
    """`threading` stand-in whose Thread runs inline; Lock is real."""
    Thread = _InlineThread
    Lock = threading.Lock
    RLock = threading.RLock


def _drive(tmp_path, gather_reply, *, turn_before=0, first_turn_cloud=None,
           redact=None, user_text=None, create_agent=True, extra_ns=None,
           reviews_shown=None):
    """POST one /chat create_agent turn through the real handler.

    ``gather_reply`` is the model's answer, or a list of its answers to
    successive asks in this turn (the last one repeats).  ``reviews_shown``
    maps the sessions a review was shown to onto what it showed; by default
    the model's own review was shown in this session, so a completed config
    is the one the person confirmed.  Pass the same dict to several calls to
    drive several turns of one session.

    Returns (response_json, chat_messages_stub, memory_stub, gather_calls).
    """
    app = flask.Flask('gather_mirror_test')
    persist = MagicMock()
    chat_messages = MagicMock(persist_and_publish_async=persist)
    memory = MagicMock()
    graph = MagicMock()
    gather_calls = []
    replies = gather_reply if isinstance(gather_reply, list) else [gather_reply]
    if reviews_shown is None:
        reviews_shown = {f'{USER_ID}_{PROMPT_ID}': None}

    def gather_info(user_id, user_message, prompt_id, autonomous=False):
        gather_calls.append(user_message)
        return replies[min(len(gather_calls), len(replies)) - 1]

    def pooled_get(url, **kw):
        if first_turn_cloud is None:
            raise ConnectionError('cloud DB offline in test')
        return MagicMock(json=lambda: first_turn_cloud)

    ns = {
        '__name__': 'hie_lifted',
        'os': os, 're': re, 'json': json, 'time': time, 'logging': logging,
        'threading': _Inline,
        'request': flask.request, 'g': flask.g, 'jsonify': flask.jsonify,
        'app': app,
        'thread_local_data': MagicMock(),
        '_cleanup_stale_agents': lambda: None,
        '_persist_language': lambda lang: None,
        'with_local_fallback': lambda cfg: cfg,
        'PROMPTS_DIR': str(tmp_path),
        '_get_user_lock': lambda uid: threading.Lock(),
        'review_agents': {}, 'conversation_agent': {},
        '_touch_agent_timestamp': lambda key: None,
        '_next_prompt_id': lambda: 'generated',
        'first_promts': [],
        'pooled_get': pooled_get,
        'pooled_post': MagicMock(),
        'DB_URL': 'http://db.invalid',
        '_gather_turn_counts': {f'{USER_ID}_{PROMPT_ID}': turn_before},
        '_gather_reviews_shown': reviews_shown,
        'MAX_GATHER_TURNS': 12,
        'retrieve_json': json.loads,
        '_record_lifecycle': lambda *a, **k: None,
        '_push_workflow_flowchart': lambda *a, **k: None,
        '_tts_synthesize_and_publish': lambda *a, **k: None,
        'get_memory': lambda user_id=None: memory,
        '_get_or_create_graph': lambda *a, **k: graph,
    }
    ns.update(extra_ns or {})
    exec(compile(_lift({'chat', '_chat_reply', '_config_is_buildable',
                        '_EMPTY_BUILD_REPLY'},
                       optional={'_decode_config_lists',
                                 '_CONFIG_LIST_FIELDS', '_read_gather_config',
                                 '_BUILDABLE_REASK', '_config_review_text',
                                 '_STEP_NUMBER', '_step_text',
                                 '_config_steps'}),
                 _SRC, 'exec'), ns)

    social = MagicMock(chat_messages=chat_messages)
    stubs = {
        'integrations.social': social,
        'integrations.social.chat_messages': chat_messages,
        'integrations.social.rate_limiter': MagicMock(
            _limiter=MagicMock(check=lambda *a, **k: True)),
        'integrations.agent_engine.dispatch': MagicMock(
            is_genuine_user_request=lambda rid: True),
        'integrations.agent_engine.budget_gate': MagicMock(
            estimate_llm_cost_spark=lambda p: 0),
        'security.hive_guardrails': MagicMock(GuardrailEnforcer=MagicMock(
            before_dispatch=lambda p: (True, '', p))),
        'security.secret_redactor': MagicMock(
            redact_secrets=redact or (lambda p: (p, 0))),
        'security.prompt_guard': MagicMock(
            check_prompt_injection=lambda p: (True, '')),
        'core.recipe_sync': MagicMock(pull_recipe=lambda *a: False),
        # _chat_reply names where the reply came from through the ONE rule
        # (core.constants.canonical_served_by): the stub passes the REAL
        # function through, so this isolation never re-implements it and the
        # lifted _chat_reply gets a string, not an auto-made MagicMock that
        # jsonify cannot serialize.
        'core.constants': MagicMock(NON_LATIN_SCRIPT_LANGS=frozenset(),
                                    canonical_served_by=_canonical_served_by),
        'core.user_lang': MagicMock(get_preferred_lang=lambda: 'en'),
        'core.teacher_avatar': MagicMock(avatar_id_from=lambda v: None),
        'hartos.gather_agentdetails': MagicMock(gather_info=gather_info),
        'langchain_classic': MagicMock(),
        'langchain_classic.schema': MagicMock(),
        'langchain_classic.schema.messages': MagicMock(),
    }
    body = {'user_id': USER_ID, 'prompt_id': PROMPT_ID,
            'prompt': USER_TEXT if user_text is None else user_text,
            'create_agent': create_agent, 'request_id': 'rq-gather-1',
            'media_mode': 'text', 'preferred_lang': 'en'}
    with patch.dict(sys.modules, stubs), patch.dict(
            os.environ, {'HEVOLVE_API_KEY': '', 'HEVOLVE_NODE_TIER': 'flat',
                         'HEVOLVE_REQUIRE_AUTH': ''}):
        with app.test_request_context('/chat', method='POST', json=body):
            resp = ns['chat']()
            payload = resp.get_json()
    return payload, persist, memory, gather_calls


def _rows(persist):
    return [(c.args[1], c.args[2]) for c in persist.call_args_list]


# Each gather-phase exit of chat(), driven by the gather_info reply that
# selects it, with the Agent_status that proves THAT exit answered.
_EXITS = [
    pytest.param('{"status": "pending", "question": "What should I call it?"}',
                 0, 'Creation Mode', 'What should I call it?', id='pending'),
    pytest.param('{"status": "completed", "flows": [{"actions": []}]}',
                 0, 'Creation Mode', None, id='empty_build'),
    pytest.param(json.dumps({'status': 'completed', 'name': 'News',
                             'flows': [{'flow_name': 'main',
                                        'actions': ['fetch news']}]}),
                 0, 'Review Mode',
                 'Got Agent details successfully lets move on to review them '
                 'one at a time', id='completed'),
    pytest.param(json.dumps({'status': 'completed', 'name': 'News',
                             'personas': json.dumps([{'name': 'Reader'}]),
                             'flows': json.dumps([{'flow_name': 'main',
                                                   'persona': 'Reader',
                                                   'actions': ['fetch news']}])}),
                 0, 'Review Mode',
                 'Got Agent details successfully lets move on to review them '
                 'one at a time', id='completed_lists_as_json_text'),
    pytest.param('Context size has been exceeded', 0, 'Review Mode',
                 'Agent created with available details. Moving to review.',
                 id='salvage'),
    pytest.param('sorry, no json here', 0, 'Creation Mode',
                 'sorry, no json here', id='parse_error'),
]


@pytest.mark.parametrize('gather_reply,turn_before,status,reply', _EXITS)
def test_gather_exit_writes_both_sides(tmp_path, gather_reply, turn_before,
                                       status, reply):
    payload, persist, memory, _ = _drive(tmp_path, gather_reply,
                                         turn_before=turn_before)
    assert payload['Agent_status'] == status, payload
    if reply is not None:
        assert payload['response'] == reply, payload

    rows = _rows(persist)
    assert ('user', USER_TEXT) in rows, (
        f'the gather turn wrote {rows}: the user side of the creation '
        f'conversation never reached the conversation mirror (GR7)')
    assert ('assistant', payload['response']) in rows, rows
    memory.save_context.assert_called_once()
    assert memory.save_context.call_args.args[0] == {'input': USER_TEXT}


# LIVE 2026-10-08 20:39:45 (agent 54, user 10202, request a54-8d833da2489c),
# shortened: the gather model wrote every list field of the confirmed config
# as JSON text.  The content was right (one flow, three plain-text actions);
# only the encoding was off, and the config was refused as unbuildable.
LIVE_LISTS_AS_JSON_TEXT = {
    'status': 'completed', 'name': 'Personalised Learning Tutor',
    'agent_name': 'teach.local.radha', 'broadcast_agent': False,
    'personas': '[ { "name": "Tutor", "description": "A patient teacher." } ]',
    'tools': '[ "get_user_id", "get_data_by_key", "save_data_in_memory" ]',
    'flows': ('[ { "flow_name": "teach", "persona": "Tutor", "actions": '
              '["1. Call get_user_id, then get_data_by_key.", '
              '"2. Write the reply to the learner yourself.", '
              '"3. Call save_data_in_memory."], '
              '"sub_goal": "every learner gets the next step" } ]'),
    'goal': 'teach each learner from their own books',
}


def test_a_config_whose_lists_came_as_json_text_is_saved_with_lists(tmp_path):
    """The saved config carries the lists the model meant, so the build and
    every reader of personas/flows get lists, not text."""
    payload, _, _, _ = _drive(tmp_path, json.dumps(LIVE_LISTS_AS_JSON_TEXT))
    assert payload['Agent_status'] == 'Review Mode', payload
    saved = json.loads((tmp_path / f'{PROMPT_ID}.json').read_text())
    assert saved['personas'] == [{'name': 'Tutor',
                                  'description': 'A patient teacher.'}]
    assert saved['tools'] == ['get_user_id', 'get_data_by_key',
                              'save_data_in_memory']
    assert [f['flow_name'] for f in saved['flows']] == ['teach']
    assert len(saved['flows'][0]['actions']) == 3
    assert saved['flows'][0]['sub_goal'] == 'every learner gets the next step'


# #205.  LIVE 2026-10-08 (agent 54, the 4B): of seven interviews one gave a
# config that could be built.  Attempt 6 sent "completed" with no review ever
# shown, and twenty steps the person never saw were saved and built.  At
# 18:47 the confirmed three steps came back with the flow's sub_goal object in
# place of the third, and the person was asked to type the steps again.
_NEWS = {'status': 'completed', 'name': 'News', 'goal': 'daily news',
         'flows': [{'flow_name': 'main', 'persona': 'Reader',
                    'actions': ['fetch news', 'summarise it']}]}
_REFUSED = {'status': 'completed', 'name': 'Tutor',
            'flows': [{'flow_name': 'teach',
                       'actions': ['read the book', {'sub_goal': 'teach'}]}]}
_FIXED = {'status': 'completed', 'name': 'Tutor',
          'flows': [{'flow_name': 'teach',
                     'actions': ['read the book', 'write the lesson']}]}


def _saved(tmp_path):
    path = tmp_path / f'{PROMPT_ID}.json'
    return json.loads(path.read_text()) if path.exists() else None


def test_a_config_completed_before_any_review_is_shown_not_built(tmp_path):
    """The person confirms what gets built: a completed config that comes
    before any review is shown to them step by step and nothing is saved.
    Their yes on the next turn builds it."""
    shown = {}
    payload, _, _, gather_calls = _drive(tmp_path, json.dumps(_NEWS),
                                         reviews_shown=shown)
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert _saved(tmp_path) is None
    assert '1. fetch news' in payload['response'], payload['response']
    assert '2. summarise it' in payload['response'], payload['response']
    assert len(gather_calls) == 1, gather_calls

    payload, _, _, _ = _drive(tmp_path, json.dumps(_NEWS), turn_before=1,
                              user_text='yes', reviews_shown=shown)
    assert payload['Agent_status'] == 'Review Mode', payload
    assert _saved(tmp_path)['flows'] == _NEWS['flows']
    assert not shown, 'a saved agent leaves no review mark for the next one'


def test_a_config_that_changed_after_its_review_is_shown_again(tmp_path):
    """The yes builds the steps that were shown.  A model that adds a step
    between the review and its completed config (the attempt 6 shape: steps
    the person never saw) gets the new config shown, not built; the next
    completed config with those same steps is built."""
    shown = {}
    _drive(tmp_path, json.dumps(_NEWS), reviews_shown=shown)
    changed = json.loads(json.dumps(_NEWS))
    changed['flows'][0]['actions'].append('email it to everyone')
    payload, _, _, _ = _drive(tmp_path, json.dumps(changed), turn_before=1,
                              user_text='yes', reviews_shown=shown)
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert '3. email it to everyone' in payload['response'], payload
    assert _saved(tmp_path) is None

    payload, _, _, _ = _drive(tmp_path, json.dumps(changed), turn_before=2,
                              user_text='yes', reviews_shown=shown)
    assert payload['Agent_status'] == 'Review Mode', payload
    assert len(_saved(tmp_path)['flows'][0]['actions']) == 3


def test_the_same_steps_numbered_or_spaced_differently_are_the_ones_shown(
        tmp_path):
    """A model that numbers or spaces the confirmed steps differently on the
    yes turn sent the same steps: built, not shown again."""
    shown = {}
    _drive(tmp_path, json.dumps(_NEWS), reviews_shown=shown)
    renumbered = json.loads(json.dumps(_NEWS))
    renumbered['flows'][0]['actions'] = ['1. fetch  news', '2)  summarise it']
    payload, _, _, _ = _drive(tmp_path, json.dumps(renumbered), turn_before=1,
                              user_text='yes', reviews_shown=shown)
    assert payload['Agent_status'] == 'Review Mode', payload


def test_steps_that_carry_their_own_number_are_numbered_once(tmp_path):
    """The live agent 54 steps begin "1. Call get_user_id ..." (see
    LIVE_LISTS_AS_JSON_TEXT); the review numbers them once."""
    cfg = {'status': 'completed', 'name': 'Tutor',
           'flows': [{'flow_name': 'teach', 'actions': [
               '1. Call get_user_id, then get_data_by_key.',
               '2. Write the reply to the learner yourself.']}]}
    payload, _, _, _ = _drive(tmp_path, json.dumps(cfg), reviews_shown={})
    assert '1. Call get_user_id' in payload['response'], payload['response']
    assert '2. Write the reply' in payload['response'], payload['response']
    assert '1. 1.' not in payload['response'], payload['response']


def test_a_step_that_begins_with_a_decimal_keeps_it(tmp_path):
    """Only a step's own number is dropped (peer hartos-77): "1.5 litres"
    is the step's text."""
    cfg = {'status': 'completed', 'name': 'Cook',
           'flows': [{'flow_name': 'cook', 'actions': [
               '1.5 litres of water, then boil it', 'add the rice']}]}
    payload, _, _, _ = _drive(tmp_path, json.dumps(cfg), reviews_shown={})
    assert '1. 1.5 litres of water' in payload['response'], payload['response']


def test_the_models_own_review_is_the_review(tmp_path):
    """A review the model showed (pending + review_details) counts: the
    completed config after the person's yes is built with no second review."""
    shown = {}
    review = 'News agent. Steps: 1. fetch news 2. summarise it'
    payload, _, _, _ = _drive(
        tmp_path, json.dumps({'status': 'pending', 'review_details': review}),
        reviews_shown=shown)
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert payload['response'] == review

    payload, _, _, _ = _drive(tmp_path, json.dumps(_NEWS), turn_before=1,
                              user_text='yes', reviews_shown=shown)
    assert payload['Agent_status'] == 'Review Mode', payload


def test_a_refused_config_is_asked_of_the_model_before_the_person(tmp_path):
    """The model wrote the confirmed steps in a shape that cannot be built;
    it is asked once more in the same turn, and its second answer is built.
    The person is not asked to type the steps again."""
    payload, _, _, gather_calls = _drive(
        tmp_path, [json.dumps(_REFUSED), json.dumps(_FIXED)])
    assert payload['Agent_status'] == 'Review Mode', payload
    assert len(gather_calls) == 2, gather_calls
    assert gather_calls[0] == USER_TEXT
    assert _saved(tmp_path)['flows'] == _FIXED['flows']


def test_the_model_is_asked_once_then_the_person(tmp_path):
    payload, _, _, gather_calls = _drive(tmp_path, json.dumps(_REFUSED))
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert len(gather_calls) == 2, gather_calls
    assert _saved(tmp_path) is None


def test_an_unreadable_second_answer_asks_the_person(tmp_path):
    """The person gets the ask for the steps, never the model's raw text."""
    payload, _, _, gather_calls = _drive(
        tmp_path, [json.dumps(_REFUSED), 'not json at all'])
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert 'not json at all' not in payload['response'], payload
    assert len(gather_calls) == 2, gather_calls
    assert _saved(tmp_path) is None


def test_a_question_in_the_models_second_answer_reaches_the_person(tmp_path):
    payload, _, _, _ = _drive(tmp_path, [
        json.dumps(_REFUSED),
        json.dumps({'status': 'pending', 'question': 'Which book first?'})])
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert payload['response'] == 'Which book first?'


def test_a_config_the_model_fixed_is_still_shown_before_it_is_built(tmp_path):
    payload, _, _, _ = _drive(
        tmp_path, [json.dumps(_REFUSED), json.dumps(_FIXED)],
        reviews_shown={})
    assert payload['Agent_status'] == 'Creation Mode', payload
    assert '2. write the lesson' in payload['response'], payload['response']
    assert _saved(tmp_path) is None


def test_a_question_from_the_model_is_asked_of_the_person_once(tmp_path):
    """Only a completed config that cannot be built goes back to the model.
    A question (pending, no flows) is the model's turn to ask the person: it
    reaches them as it is, and the model is not asked again (a second answer
    would replace the question)."""
    payload, _, _, gather_calls = _drive(tmp_path, [
        json.dumps({'status': 'pending', 'question': 'What should I call it?'}),
        json.dumps(_FIXED)])
    assert payload['response'] == 'What should I call it?', payload
    assert len(gather_calls) == 1, gather_calls
    assert _saved(tmp_path) is None


def test_the_last_turn_asks_the_model_once(tmp_path):
    """The last turn is the forced completion, unchanged by #205: one ask,
    the wrap-up instruction, and no second ask of the model."""
    payload, _, _, gather_calls = _drive(
        tmp_path, [json.dumps(_REFUSED), json.dumps(_FIXED)], turn_before=11)
    assert len(gather_calls) == 1, gather_calls
    assert gather_calls[0].startswith('Please finalize'), gather_calls
    assert payload['Agent_status'] == 'Review Mode', payload


def test_first_turn_records_the_users_words_not_the_augmented_prompt(tmp_path):
    """gather_info gets the cloud record appended; the mirror must not."""
    _, persist, memory, gather_calls = _drive(
        tmp_path, '{"status": "pending", "question": "Name?"}',
        first_turn_cloud=[{'name': 'Daily News', 'prompt': 'summarise'}])
    assert gather_calls and 'name:Daily News' in gather_calls[0], gather_calls
    user_rows = [t for r, t in _rows(persist) if r == 'user']
    assert user_rows == [USER_TEXT], user_rows
    assert memory.save_context.call_args.args[0] == {'input': USER_TEXT}


def test_forced_completion_turn_records_the_users_words(tmp_path):
    """On the last turn gather_info gets the wrap-up instruction instead."""
    _, persist, _, gather_calls = _drive(
        tmp_path, json.dumps({'status': 'completed',
                              'flows': [{'actions': ['a']}]}),
        turn_before=11)
    assert gather_calls[0].startswith('Please finalize'), gather_calls
    user_rows = [t for r, t in _rows(persist) if r == 'user']
    assert user_rows == [USER_TEXT], user_rows


def test_the_recorded_and_gathered_text_is_the_redacted_text(tmp_path):
    """Review of 8c9abe070, measured: the gather path re-read the raw request
    body after /chat's guardrail and redact_secrets pass, so a key the user
    typed reached gather_info's LLM and the conversation mirror, whose
    publish leg goes to peers.  Both must get what the gates left."""
    secret = 'agent using key sk-SECRET to summarise news'

    def redact(p):
        return p.replace('sk-SECRET', '[REDACTED:key]'), 1

    _, persist, memory, gather_calls = _drive(
        tmp_path, '{"status": "pending", "question": "Name?"}',
        redact=redact, user_text=secret)
    user_rows = [t for r, t in _rows(persist) if r == 'user']
    assert user_rows == ['agent using key [REDACTED:key] to summarise news'], (
        user_rows)
    assert gather_calls and all('sk-SECRET' not in c for c in gather_calls), (
        gather_calls)
    assert 'sk-SECRET' not in str(memory.save_context.call_args)


def test_the_final_answer_path_gets_the_redacted_text_too(tmp_path):
    """Same re-read on the non-create path (the get_ans call): it restored
    data['prompt'], so the main LLM answer saw the raw key as well."""
    seen = []

    class _Reached(Exception):
        """What get_ans received is the whole claim; the reply tail after it
        is not under test, so the stub stops the route there."""

    def get_ans(casual_conv, req_tool, user_id=None, query=None, **kw):
        seen.append(query)
        raise _Reached()

    def redact(p):
        return p.replace('sk-SECRET', '[REDACTED:key]'), 1

    # A system agent's turn is the one chat() sends straight to get_ans.
    (tmp_path / f'{PROMPT_ID}.json').write_text(json.dumps({
        'is_system_agent': True, 'name': 'livetest_system',
        'flows': [{'system_prompt': 'You are helpful.'}]}))
    with pytest.raises(_Reached):
        _drive(tmp_path, 'unused', redact=redact, user_text='check sk-SECRET',
               create_agent=False, extra_ns={'get_ans': get_ans,
                                             'publish_async': MagicMock()})
    assert seen == ['check [REDACTED:key]'], seen
