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


def _lift(names):
    """Source of the named top-level defs / assignments, in file order."""
    src = open(_SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append(ast.get_source_segment(src, node))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in names):
            out.append(ast.get_source_segment(src, node))
    found = len(out)
    assert found == len(names), (
        f'lifted {found} of {sorted(names)} from {_SRC} -- re-point this '
        f'test rather than deleting it')
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
           redact=None, user_text=None, create_agent=True, extra_ns=None):
    """POST one /chat create_agent turn through the real handler.

    Returns (response_json, chat_messages_stub, memory_stub, gather_calls).
    """
    app = flask.Flask('gather_mirror_test')
    persist = MagicMock()
    chat_messages = MagicMock(persist_and_publish_async=persist)
    memory = MagicMock()
    graph = MagicMock()
    gather_calls = []

    def gather_info(user_id, user_message, prompt_id, autonomous=False):
        gather_calls.append(user_message)
        return gather_reply

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
                        '_EMPTY_BUILD_REPLY'}), _SRC, 'exec'), ns)

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
