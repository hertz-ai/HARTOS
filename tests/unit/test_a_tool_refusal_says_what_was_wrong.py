"""A tool's refusal tells the model what was wrong and what to do next.

The owner, 2026-11-09: "all refusals shd hint llm on what was wrong with tool
call", then "enforce a framework for tool call so that validation errors are
semantically understood by LLM so that retry can correct them".

Measured that day (desktop llm_outbound.jsonl + .old): 482 tool calls; 131
refused by the tool itself after the argument check passed; of the 113
retries that followed, 92 failed again, 33 with identical arguments.  The
refusals were codes or bare facts ({"refused": "too_many_open_asks"} 66
times) and the model was never told what to change.

What these tests hold, each by calling the real code:
  * core.tool_refusal.refuse / refusal_fields build a refusal that names the
    problem, the fix and whether a retry can help, in the failed shape the
    pipeline already reads (core.constants.tool_reply_failed);
  * a JSON reply whose top-level status is 'refused' is a failed call, so the
    CREATE banker does not bank it as a recipe step (it did: ask_person's
    refusal opened with '{' and neither failure rule saw it);
  * core.tool_logging.log_tool_execution, which every registered tool returns
    through, logs one [TOOL-REFUSAL-NO-HINT] line for a failed reply that
    carries no hint, and none for a hinted refusal or a real result;
  * the envelope for a tool that RAISED tells the model what to do instead of
    "Check logs", which it cannot read.
"""
import asyncio
import json
import logging
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.tool_logging as tl  # noqa: E402
from core.constants import (  # noqa: E402
    TOOL_EXECUTION_FAILED_PREFIX, tool_reply_failed)
from core.tool_logging import log_tool_execution  # noqa: E402
from core.tool_refusal import (  # noqa: E402
    NOTE_KEY, RETRY_AFTER_FIX, RETRY_LATER, RETRY_NEVER, is_json_refusal,
    refusal_fields, refusal_has_hint, refuse)
from tests.unit.test_trace_action_banking import banked  # noqa: E402,F401

NO_HINT = '[TOOL-REFUSAL-NO-HINT]'

# ask_person's refusal as HEAD writes it (person_ask.py, the tool reply).
ASK_REFUSED = json.dumps({'answered': False, 'refused': 'too_many_open_asks',
                          'status': 'refused'})


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture()
def tool_log(monkeypatch):
    """Every line the tool logger writes, without the UI emit or the vault
    (both reach outside the process)."""
    monkeypatch.setattr(tl, '_emit_tool_call_stage', lambda name: None)
    monkeypatch.setattr(tl, 'credential_vault', lambda: None)
    handler = _Lines()
    logger = logging.getLogger('agent_logger')
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _no_hint_lines(lines):
    return [line for line in lines if NO_HINT in line]


# ---------------------------------------------------------------------------
# The refusal a tool builds.
# ---------------------------------------------------------------------------
def test_a_refusal_names_the_problem_the_fix_and_the_retry():
    reply = refuse('get_data_by_key', 'no value is stored under teach.10202',
                   'call get_saved_keys to see which keys exist')
    assert reply.startswith('Error: the get_data_by_key call could not be done: ')
    assert 'no value is stored under teach.10202.' in reply
    assert 'call get_saved_keys to see which keys exist.' in reply
    assert 'Call get_data_by_key again only with that changed' in reply
    assert tool_reply_failed(reply), reply
    assert refusal_has_hint(reply), reply


@pytest.mark.parametrize('retry,words', [
    (RETRY_AFTER_FIX, 'again only with that changed'),
    (RETRY_LATER, 'do not call ask_person again now'),
    (RETRY_NEVER, 'tell the person what could not be done'),
])
def test_each_retry_mode_says_whether_calling_again_can_help(retry, words):
    reply = refuse('ask_person', 'three questions are already waiting',
                   'wait for an answer', retry=retry)
    assert words in reply, reply


@pytest.mark.parametrize('problem,fix,retry', [
    ('', 'call get_user_id first', RETRY_AFTER_FIX),
    ('   ', 'call get_user_id first', RETRY_AFTER_FIX),
    ('the id is missing', '', RETRY_AFTER_FIX),
    ('the id is missing', None, RETRY_AFTER_FIX),
    ('the id is missing', 'call get_user_id first', 'maybe'),
])
def test_a_refusal_without_a_problem_a_fix_or_a_known_retry_is_not_built(
        problem, fix, retry):
    with pytest.raises(ValueError):
        refuse('get_user_id', problem, fix, retry=retry)


def test_a_json_refusal_carries_the_same_sentence_under_its_note():
    fields = refusal_fields('ask_person', 'too_many_open_asks',
                            'three questions are already waiting',
                            'wait for an answer', retry=RETRY_LATER)
    assert fields['status'] == 'refused'
    assert fields['refused'] == 'too_many_open_asks'
    assert fields['retry'] == RETRY_LATER
    assert fields[NOTE_KEY] == refuse('ask_person',
                                      'three questions are already waiting',
                                      'wait for an answer', retry=RETRY_LATER)
    reply = json.dumps({'answered': False, **fields})
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply


# ---------------------------------------------------------------------------
# The one failure rule reads a JSON refusal.
# ---------------------------------------------------------------------------
def test_a_json_reply_with_status_refused_is_a_failed_call():
    assert is_json_refusal(ASK_REFUSED)
    assert tool_reply_failed(ASK_REFUSED), ASK_REFUSED
    assert tool_reply_failed('\n ' + ASK_REFUSED + ' \n')


@pytest.mark.parametrize('reply', [
    # A person's "no" is an answer, not a refused call.
    json.dumps({'answered': True, 'status': 'answered', 'answer': 'no'}),
    # One refused item inside a results list is that item's outcome.
    json.dumps({'status': 'ok', 'results': [{'status': 'refused'}]}),
    json.dumps([{'status': 'refused'}]),
    # Text that quotes the shape is a result.
    'The log said {"status": "refused"} yesterday.',
    '{"status": "refused"',
    '{"status": "REFUSED"}',
])
def test_a_reply_that_only_mentions_a_refusal_is_not_a_failed_call(reply):
    assert not tool_reply_failed(reply), reply


def test_a_refused_ask_is_not_banked_as_a_recipe_step(banked):
    """CREATE's banker reads the one rule: a refused ask_person call did
    nothing, so the saved recipe must not tell REUSE to repeat it."""
    ok, data, _ = banked([
        {'content': 'Execute Action 2: ask the parent which day suits them'},
        {'content': '', 'tool_calls': [{'id': 'a1', 'function': {
            'name': 'ask_person',
            'arguments': '{"question": "Which day suits you?"}'}}]},
        {'content': ASK_REFUSED, 'role': 'tool', 'tool_responses': [
            {'tool_call_id': 'a1', 'role': 'tool', 'content': ASK_REFUSED}]},
    ])
    assert ok is True
    assert 'ask_person' not in [s['tool_name'] for s in data['recipe']], data


# ---------------------------------------------------------------------------
# Enforcement: every registered tool's reply passes log_tool_execution.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('reply', [
    'Error: Key not usable.',
    ASK_REFUSED,
])
def test_a_failed_reply_with_no_hint_is_counted_once(tool_log, reply):
    @log_tool_execution
    def ask_person(question):
        return reply

    assert ask_person('Which day?') == reply      # the reply is not changed
    lines = _no_hint_lines(tool_log)
    assert len(lines) == 1, tool_log
    assert 'ask_person' in lines[0], lines


@pytest.mark.parametrize('reply', [
    refuse('ask_person', 'three questions are already waiting',
           'wait for an answer', retry=RETRY_LATER),
    json.dumps({'answered': False, **refusal_fields(
        'ask_person', 'too_many_open_asks', 'three questions are already '
        'waiting', 'wait for an answer', retry=RETRY_LATER)}),
    # A JSON refusal whose note a tool wrote in its own words, not through
    # refuse() (person_ask's notes): the note is the hint.
    json.dumps({'answered': False, 'refused': 'too_many_open_asks',
                'status': 'refused',
                'note': 'Three questions to this person are already waiting; '
                        'wait for one to be answered before asking again.'}),
    # An older failure text with the hint appended still says what to do.
    'Not able to perform this action now please try later\n' + refuse(
        'ask_person', 'the companion app is closed', 'open it first'),
    'Saved at teach.10202',
    json.dumps({'answered': True, 'status': 'answered', 'answer': 'Friday'}),
])
def test_a_hinted_refusal_or_a_real_result_is_not_counted(tool_log, reply):
    @log_tool_execution
    def ask_person(question):
        return reply

    assert ask_person('Which day?') == reply
    assert _no_hint_lines(tool_log) == [], tool_log


def test_an_async_tool_is_counted_the_same_way(tool_log):
    @log_tool_execution
    async def ask_person(question):
        return ASK_REFUSED

    assert asyncio.run(ask_person('Which day?')) == ASK_REFUSED
    assert len(_no_hint_lines(tool_log)) == 1, tool_log


def test_the_count_never_breaks_the_tool(tool_log, monkeypatch):
    """It sits on every tool's return path: a fault in the check is a debug
    line, never a lost reply."""
    import core.constants as constants

    def broken(_content):
        raise RuntimeError('rule unavailable')

    monkeypatch.setattr(constants, 'tool_reply_failed', broken)

    @log_tool_execution
    def ask_person(question):
        return 'Error: Key not usable.'

    assert ask_person('Which day?') == 'Error: Key not usable.'
    assert _no_hint_lines(tool_log) == [], tool_log


# ---------------------------------------------------------------------------
# A tool that raised.
# ---------------------------------------------------------------------------
def test_a_tool_that_raised_tells_the_model_what_to_do(tool_log):
    @log_tool_execution
    def list_book_chapters(title):
        raise KeyError(title)

    reply = list_book_chapters('The Art of Computer Programming')
    assert reply.startswith(TOOL_EXECUTION_FAILED_PREFIX)
    assert tool_reply_failed(reply), reply
    payload = json.loads(reply[len(TOOL_EXECUTION_FAILED_PREFIX):])
    suggestion = payload['suggestion']
    assert 'log' not in suggestion.lower(), suggestion
    assert 'list_book_chapters' in suggestion, suggestion
    assert 'do not repeat it' in suggestion, suggestion


# ---------------------------------------------------------------------------
# core/agent_tools' own refusals, called through the real tool closures.
# ---------------------------------------------------------------------------
from tests.unit.test_a_memory_key_with_a_placeholder_is_refused import (  # noqa: E402
    LEARNER, PROGRESS, _store, _tools, store_dir)  # noqa: F401

# Agents' saved steps quote this sentence ("'Key not found' means a new
# learner"), so the answer keeps it first.
KEY_NOT_FOUND = 'Key not found in stored data.'


def test_a_read_of_an_unsaved_key_names_the_keys_that_are_saved(store_dir):
    tools = _tools(_store())
    tools['save_data_in_memory'](f'teach.{LEARNER}', PROGRESS)
    reply = tools['get_data_by_key']('teach.10203')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert f'The keys saved under teach are: {LEARNER}.' in reply, reply
    # A key nobody saved yet is how a first visit looks: an answer, not a
    # failed call, so a step that reads it is not re-steered.
    assert not tool_reply_failed(reply), reply


def test_a_first_visit_read_says_nothing_is_saved_yet(store_dir):
    reply = _tools(_store())['get_data_by_key']('teach')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert 'Nothing is saved in this memory yet.' in reply, reply
    assert 'first visit' in reply, reply
    assert not tool_reply_failed(reply), reply


def test_a_read_through_a_single_value_says_to_read_the_value(store_dir):
    tools = _tools(_store())
    tools['save_data_in_memory']('name', 'Ada')
    reply = tools['get_data_by_key']('name.first')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert 'name holds a value, not keys under it: read name itself.' in reply


@pytest.mark.parametrize('tool', ['save_data_in_memory', 'get_data_by_key'])
def test_a_placeholder_key_refusal_says_what_to_change(store_dir, tool):
    tools = _tools(_store())
    args = ('teach.<user_id>', PROGRESS) if tool == 'save_data_in_memory' \
        else ('teach.<user_id>',)
    reply = tools[tool](*args)
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert f'Call {tool} again only with that changed' in reply, reply


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = 'a red bicycle'


@pytest.fixture
def vision(monkeypatch):
    """The image tool's three outside calls: the URL filter, the vision
    endpoint and the HTTP post."""
    import core.agent_tools as agent_tools
    import core.config_cache as config_cache
    import security.sanitize as sanitize
    monkeypatch.setattr(sanitize, 'validate_url', lambda url: url)
    monkeypatch.setattr(config_cache, 'get_vision_api', lambda: 'http://vision.test/caption')
    monkeypatch.setattr(config_cache, 'is_bundled', lambda: True)
    answers = []
    monkeypatch.setattr(agent_tools.requests, 'post',
                        lambda *a, **k: _Response(answers.pop(0)))
    return answers


@pytest.mark.parametrize('status,words', [
    (404, 'HTTP 404'),
    (403, 'HTTP 403'),
])
def test_an_image_link_the_service_cannot_read_says_to_fix_the_link(
        store_dir, vision, status, words):
    vision.append(status)
    reply = _tools(_store())['get_text_from_image']('http://example.test/a.png')
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert words in reply and 'direct link to an image file' in reply, reply
    assert 'again only with that changed' in reply, reply


def test_a_failing_image_service_says_not_to_call_again_now(store_dir, vision):
    vision.append(503)
    reply = _tools(_store())['get_text_from_image']('http://example.test/a.png')
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert 'HTTP 503' in reply and 'do not call get_text_from_image again now' in reply


def test_an_image_the_service_read_is_returned_as_it_came(store_dir, vision):
    vision.append(200)
    assert _tools(_store())['get_text_from_image']('http://example.test/a.png') \
        == 'a red bicycle'


@pytest.fixture
def executor(monkeypatch):
    """The patched autogen executor (hartos.helper.force_apply_autogen_json_fix),
    the path every model tool call takes; restored afterwards."""
    from autogen.agentchat.conversable_agent import ConversableAgent
    from hartos.helper import force_apply_autogen_json_fix
    original = (ConversableAgent.execute_function,
                ConversableAgent.a_execute_function)
    assert force_apply_autogen_json_fix()
    monkeypatch.setattr(tl, 'credential_vault', lambda: None)
    yield ConversableAgent
    (ConversableAgent.execute_function,
     ConversableAgent.a_execute_function) = original


def _executed(agent_cls, tools, name, arguments='{}', run_async=False):
    """One model call named ``name``, run by an agent holding ``tools``;
    returns the reply the model reads."""
    asker = agent_cls('asker', llm_config=False, human_input_mode='NEVER')
    runner = agent_cls('runner', llm_config=False, human_input_mode='NEVER')
    runner.register_function({n: runner._wrap_function(f) for n, f in tools.items()})
    asker.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': name, 'arguments': arguments}}]},
               runner, request_reply=False, silent=True)
    if run_async:
        _, reply = asyncio.run(runner.a_generate_tool_calls_reply(sender=asker))
    else:
        _, reply = runner.generate_tool_calls_reply(sender=asker)
    return reply['tool_responses'][0]['content']


def _two_tools():
    def get_item(id: str) -> str:
        return 'item ' + id

    def save_note(text: str) -> str:
        return 'saved'
    return {'get_item': get_item, 'save_note': save_note}


@pytest.mark.parametrize('run_async', [False, True])
def test_a_call_to_a_tool_that_does_not_exist_names_the_ones_that_do(
        executor, run_async):
    """Daemon agent 52612946585's step names huggingface_search_models, which
    exists nowhere; the reply was "Error: Function X not found."."""
    reply = _executed(executor, _two_tools(), 'huggingface_search_models',
                      run_async=run_async)
    assert reply.startswith('Error: the huggingface_search_models call could not '
                            'be done: no tool named huggingface_search_models'), reply
    assert 'by its exact name: get_item, save_note.' in reply, reply
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply


@pytest.mark.parametrize('run_async', [False, True])
def test_a_tool_that_raised_in_the_executor_says_what_to_do(executor, run_async):
    def list_book_chapters(title: str) -> str:
        raise KeyError(title)

    reply = _executed(executor, {'list_book_chapters': list_book_chapters},
                      'list_book_chapters', '{"title": "TAOCP"}', run_async=run_async)
    assert reply.startswith('Error: the list_book_chapters call could not be done: '
                            "it raised KeyError: 'TAOCP'."), reply
    assert 'tell the person what could not be done' in reply, reply
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply


def test_a_tool_that_ran_is_answered_as_before(executor):
    assert _executed(executor, _two_tools(), 'get_item', '{"id": "7"}') == 'item 7'


def test_an_image_link_the_filter_refuses_says_what_to_pass(store_dir, monkeypatch):
    import security.sanitize as sanitize

    def refuse_link(url):
        raise ValueError('private address')

    monkeypatch.setattr(sanitize, 'validate_url', refuse_link)
    reply = _tools(_store())['get_text_from_image']('http://10.0.0.5/a.png')
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert 'private address' in reply and 'public http or https link' in reply
