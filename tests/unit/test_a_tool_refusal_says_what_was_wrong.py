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
import core.tool_refusal as tool_refusal  # noqa: E402
from core.tool_refusal import (  # noqa: E402
    NOTE_KEY, RETRY_AFTER_FIX, RETRY_LATER, RETRY_NEVER, RETRY_OTHER_TOOL,
    is_json_refusal, refusal_fields, refusal_has_hint, refuse)
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
    (RETRY_OTHER_TOOL, 'There is no ask_person to call again'),
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


def test_a_reply_that_cannot_be_a_refusal_is_never_parsed(monkeypatch):
    """Both checks run on every wrapped tool's reply; the review of
    2ddf102da measured a 1.2 MB JSON reply going from 0.38 ms to 33 ms when
    each one was parsed.  A reply without the word they look for is
    answered without a parse; one with it is still parsed and read."""
    parsed = []
    real = tool_refusal._json_object

    def counting(body):
        parsed.append(len(body))
        return real(body)

    monkeypatch.setattr(tool_refusal, '_json_object', counting)
    big = json.dumps({'status': 'ok', 'rows': ['x' * 64] * 20000})
    assert not is_json_refusal(big)
    assert not refusal_has_hint(big)
    assert parsed == [], parsed
    assert is_json_refusal(ASK_REFUSED)
    assert parsed, 'a reply naming "refused" must still be read'


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
    AGENT, LEARNER, PROGRESS, _store, _tools, store_dir)  # noqa: F401
from core import cache_loaders  # noqa: E402

# Agents' saved steps quote this sentence ("'Key not found' means a new
# learner"), so the answer keeps it first.
KEY_NOT_FOUND = 'Key not found in stored data.'


def test_a_read_of_an_unsaved_key_names_the_keys_that_are_saved(store_dir):
    tools = _tools(_store())
    tools['save_data_in_memory'](f'teach.{LEARNER}', PROGRESS)
    reply = tools['get_data_by_key']('teach.10203')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert f"This person's keys under teach are: {LEARNER}." in reply, reply
    assert 'If you meant one of those, read that key.' in reply, reply
    # A key nobody saved yet is how a first visit looks: an answer, not a
    # failed call, so a step that reads it is not re-steered.
    assert not tool_reply_failed(reply), reply


def test_a_first_visit_read_says_nothing_is_saved_yet(store_dir):
    reply = _tools(_store())['get_data_by_key']('teach')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert 'This person has nothing saved in this memory yet.' in reply, reply
    assert 'first visit' in reply, reply
    # No key was named, so none is offered to read.
    assert 'read that key' not in reply, reply
    assert not tool_reply_failed(reply), reply


def test_a_read_through_a_single_value_says_it_is_a_single_value(store_dir):
    tools = _tools(_store())
    tools['save_data_in_memory']('name', 'Ada')
    reply = tools['get_data_by_key']('name.first')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert ("This person's saved name is a single value, with no keys under "
            "it.") in reply, reply
    assert 'read that key' not in reply, reply


OTHER = '96985'


@pytest.mark.parametrize('shared', [
    # Another person's record kept in the agent's shared store before the
    # per-person ruling (agent 96985165407's shared 'tutor', 11-09).
    {'teach': {OTHER: PROGRESS}},
    # The creator's own progress saved straight under 'teach' (agent 54's
    # shared 'teach', 11-09): a single value there.
    {'teach': PROGRESS},
])
def test_a_new_learner_is_never_steered_to_a_record_in_the_shared_store(
        store_dir, shared):
    """Review of 2ddf102da, F1, measured on the owner's agent_data: the
    first cut walked the whole memory view, so a new learner's read of
    teach.<id> named the shared store's keys and said to read them -- the
    creator's progress, or another person's id.  Only this person's own
    store is described."""
    cache_loaders.save_agent_data(AGENT, shared)
    tools = _tools(_store(), user_id='10203')
    reply = tools['get_data_by_key']('teach.10203')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert 'This person has nothing saved in this memory yet.' in reply, reply
    assert OTHER not in reply, reply
    assert 'single value' not in reply and 'read that key' not in reply, reply
    assert not tool_reply_failed(reply), reply


def test_a_learner_with_records_is_shown_only_their_own_keys(store_dir):
    cache_loaders.save_agent_data(AGENT, {'teach': {OTHER: PROGRESS}})
    tools = _tools(_store())
    tools['save_data_in_memory'](f'teach.{LEARNER}', PROGRESS)
    reply = tools['get_data_by_key']('teach.10203')
    assert f"This person's keys under teach are: {LEARNER}." in reply, reply
    assert OTHER not in reply, reply


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
    # Review of 2ddf102da, F3: the reply pointed back at the missing tool
    # ("Call huggingface_search_models again only with that changed").
    assert 'There is no huggingface_search_models to call again' in reply, reply
    assert 'again only with that changed' not in reply, reply
    assert 'request_tools' not in reply, reply
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply


def test_an_unknown_tool_reply_names_request_tools_when_the_agent_has_it(executor):
    tools = dict(_two_tools(), request_tools=lambda need: 'attached')
    reply = _executed(executor, tools, 'huggingface_search_models')
    assert 'get_item, request_tools, save_note' in reply, reply
    assert ('call request_tools with a short description of that capability'
            in reply), reply
    assert 'There is no huggingface_search_models to call again' in reply, reply


def test_an_unknown_tool_on_an_agent_with_no_tools_says_to_answer_in_writing():
    from hartos.helper import executor_refusal
    reply = executor_refusal('huggingface_search_models', {})
    assert 'This agent has no tools: answer in writing instead.' in reply, reply
    assert 'Calling huggingface_search_models again cannot work' in reply, reply
    assert 'one of the tool names given' not in reply, reply
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


# ---------------------------------------------------------------------------
# Enforcement in the executor: tools registered as plain callables.
# ---------------------------------------------------------------------------
# Review of 2ddf102da, F2: the count lived only in log_tool_execution, so the
# families that register plain callables (reel studio, news, finance,
# commerce, remote desktop, media, ...) never reached it -- its probe logged 0
# lines for a plain tool and 1 once the same tool was wrapped.  The patched
# executor is the path every autogen tool reply takes, so it counts what the
# wrapper did not.
@pytest.mark.parametrize('run_async', [False, True])
def test_a_plain_tools_unhinted_refusal_is_counted_by_the_executor(
        executor, tool_log, run_async):
    def list_reel_clips(project: str) -> str:
        return 'Error: project not found'

    reply = _executed(executor, {'list_reel_clips': list_reel_clips},
                      'list_reel_clips', '{"project": "p1"}', run_async=run_async)
    assert reply == 'Error: project not found'       # the reply is not changed
    lines = _no_hint_lines(tool_log)
    assert len(lines) == 1, tool_log
    assert 'list_reel_clips' in lines[0], lines


def test_an_async_plain_tool_is_counted_by_the_executor(executor, tool_log):
    async def list_reel_clips(project: str) -> str:
        return ASK_REFUSED

    reply = _executed(executor, {'list_reel_clips': list_reel_clips},
                      'list_reel_clips', '{"project": "p1"}', run_async=True)
    assert reply == ASK_REFUSED
    assert len(_no_hint_lines(tool_log)) == 1, tool_log


@pytest.mark.parametrize('run_async', [False, True])
def test_a_wrapped_tool_is_counted_once_through_the_executor(
        executor, tool_log, run_async):
    """A tool wrapped by log_tool_execution counted its own reply; the
    executor does not count it a second time."""
    @log_tool_execution
    def ask_person(question: str) -> str:
        return ASK_REFUSED

    _executed(executor, {'ask_person': ask_person}, 'ask_person',
              '{"question": "Which day?"}', run_async=run_async)
    assert len(_no_hint_lines(tool_log)) == 1, tool_log


def test_an_async_wrapped_tool_is_counted_once_through_the_executor(
        executor, tool_log):
    """An async def gets log_tool_execution's other wrapper (async_wrapper),
    which must carry the same mark, or the async executor counts its reply a
    second time.  The proof run of 2026-11-12 left that mark's removal
    (mutant E5) alive: every wrapped tool above is a plain def."""
    @log_tool_execution
    async def ask_person(question: str) -> str:
        return ASK_REFUSED

    reply = _executed(executor, {'ask_person': ask_person}, 'ask_person',
                      '{"question": "Which day?"}', run_async=True)
    assert reply == ASK_REFUSED
    assert len(_no_hint_lines(tool_log)) == 1, tool_log


@pytest.mark.parametrize('reply', [
    refuse('list_reel_clips', 'no project named p1',
           'call list_reel_projects and use one of its names'),
    'clip_1.mp4, clip_2.mp4',
])
def test_a_plain_tools_hinted_refusal_or_result_is_not_counted(
        executor, tool_log, reply):
    def list_reel_clips(project: str) -> str:
        return reply

    assert _executed(executor, {'list_reel_clips': list_reel_clips},
                     'list_reel_clips', '{"project": "p1"}') == reply
    assert _no_hint_lines(tool_log) == [], tool_log


# ---------------------------------------------------------------------------
# A message to the person that still holds a fill-in mark.
# ---------------------------------------------------------------------------
# Live 2026-11-11 01:59 IST on build 69 (hevolve-66e6's two-person proof,
# agent 1793366231447, request msg_1794342560815_sim): send_message_to_user
# delivered "Thank you, <parent_name>! ..." to the parent.  Measured on this
# desktop's llm_outbound logs (1061 send texts): the other unfilled shape is
# '[REVENUE_AMOUNT]' (one send carried five), and '<tool_call>' markup went
# out in two.  Every one is an identifier joined by '_' inside <> or [].
from core.session_cache import TTLCache as _SendStore  # noqa: E402
from tests.unit.test_a_memory_key_with_a_placeholder_is_refused import (  # noqa: E402
    _DiskHelper)


def _send_tools():
    """The real core tool closures with a sender and a scheduler that record
    what would have reached the person."""
    import core.agent_tools as agent_tools
    sent, scheduled = [], []

    class _Scheduler:
        def add_job(self, func, trigger, run_date=None, args=None):
            scheduled.append(list(args or []))

    def _sender(user_id, text, *rest, **kw):
        sent.append(text)
        return 'Message sent successfully to user with request_id: r1'

    ctx = {
        'building_agent': False, 'user_id': '10202', 'prompt_id': '54',
        'agent_data': _SendStore(ttl_seconds=60, max_size=10, name='t_send'),
        'helper_fun': _DiskHelper(), 'user_prompt': '10202_54',
        'request_id_list': {}, 'recent_file_id': None,
        'scheduler': _Scheduler(), 'send_message_to_user1': _sender,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    tools = {n: f for n, _d, f in agent_tools.build_core_tool_closures(ctx)}
    return tools, sent, scheduled


@pytest.mark.parametrize('text, placeholder', [
    ("Thank you, <parent_name>! Arjun did well today.", '<parent_name>'),
    ("Revenue this week: [REVENUE_AMOUNT] against costs of [TOTAL_COSTS].",
     '[REVENUE_AMOUNT]'),
    ("<tool_call>\n<function=google_search>\n<parameter=text>shoes",
     '<tool_call>'),
    # The whole text is one blank, the shape ask_person's example writes
    # ("<why you ask>"): one rule for every text that reaches a person
    # (review C-1 of 0516f3981).
    ("<your message to the parent>", '<your message to the parent>'),
    ("  <why you ask>\n", '<why you ask>'),
])
def test_a_message_that_still_holds_a_placeholder_is_refused_and_not_sent(
        tool_log, text, placeholder):
    tools, sent, _scheduled = _send_tools()
    reply = tools['send_message_to_user'](text, False)
    assert sent == [], 'the person must not receive the fill-in mark'
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert placeholder in reply, reply
    assert 'send_message_to_user' in reply, reply


@pytest.mark.parametrize('text', [
    "Is 3 < 5 and 7 > 2? Yes, both are true!",
    "Use the <div> tag to group the page, and <b> for bold words.",
    "See [1] for the source, and [Note] keep practising.",
    "Say 'ball' slowly: <ball> rhymes with <call>.",
    "Great try, Arjun! Now say 'buh-buh-ball' with me.",
    # A coding lesson's own text: a mark glued to the word before it is an
    # index or a type, and [words](link) is a markdown link (review C-2 of
    # 0516f3981 measured all five refused).
    "Index the list as my_list[item_index], then print it.",
    "Read each cell with row[col_name] inside the loop.",
    "Your key is read from os.environ[GITHUB_TOKEN] when it starts.",
    "Declare it as std::vector<my_type> before the loop.",
    "Open [click_here](https://example.org/lesson) to see the page.",
    "Is the parcel <2 kg?",
])
def test_a_message_without_a_placeholder_is_sent_as_written(tool_log, text):
    tools, sent, _scheduled = _send_tools()
    reply = tools['send_message_to_user'](text, False)
    assert sent == [text], reply
    assert not tool_reply_failed(reply), reply


def test_a_timed_message_with_a_placeholder_is_refused_and_not_scheduled(
        tool_log):
    tools, _sent, scheduled = _send_tools()
    reply = tools['send_message_in_seconds'](
        "Good morning, <child_name>! Ready for today's sounds?", 30)
    assert scheduled == [], scheduled
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert '<child_name>' in reply, reply


# ---------------------------------------------------------------------------
# One rule for every text a person reads: a message and an ask agree.
# ---------------------------------------------------------------------------
# Review C-1 of 0516f3981: the send tools and ask_person held two rules, so
# "Hi <parent_name>, which day suits you?" was refused as a message and sent
# as an ask, and "<why you ask>" the other way round.
def _asked(monkeypatch, text, where):
    """person_ask.ask with ``text`` as the reason or as the question's label.
    The card push is the boundary: it refuses, so an ask that passed every
    check before it reads 'not_shown' and nothing is stored or sent."""
    from integrations.agent_engine import liquid_ui_service, person_ask
    monkeypatch.setattr(liquid_ui_service, 'push_agent_ui', lambda *a, **k: False)
    reason = text if where == 'description' else 'To plan the next lesson.'
    label = text if where == 'label' else 'Which day suits you?'
    component = {'type': 'form', 'description': reason,
                 'fields': [{'name': 'answer', 'label': label, 'type': 'text'}]}
    return person_ask.ask('10202', '54', component, prompt_id='54', title='A question')


@pytest.mark.parametrize('where', ['description', 'label'])
@pytest.mark.parametrize('text, blank', [
    ("Hi <parent_name>, which day suits you?", '<parent_name>'),
    ("Revenue is [REVENUE_AMOUNT] today?", '[REVENUE_AMOUNT]'),
    ("<why you ask>", '<why you ask>'),
    ("<your question>", '<your question>'),
])
def test_a_blank_a_message_refuses_an_ask_refuses_too(
        tool_log, monkeypatch, where, text, blank):
    tools, sent, _scheduled = _send_tools()
    reply = tools['send_message_to_user'](text, False)
    assert sent == [] and blank in reply, reply
    out = _asked(monkeypatch, text, where)
    assert out['refused'] == 'unfilled_placeholder', out
    from integrations.agent_engine.person_ask import _refusal_note
    component = {'description': text if where == 'description' else 'x',
                 'fields': [{'label': text if where == 'label' else 'y'}]}
    assert blank in _refusal_note('unfilled_placeholder', component, '10202', '54', False)


@pytest.mark.parametrize('where', ['description', 'label'])
@pytest.mark.parametrize('text', [
    "Is the parcel <2 kg?",
    "To pick the box size: <30 cm or >50 cm?",
    "Use my_list[item_index] to read the third word.",
    "Say <ball> slowly, then tell me the word you heard.",
])
def test_words_a_message_sends_an_ask_asks_too(tool_log, monkeypatch, where, text):
    tools, sent, _scheduled = _send_tools()
    tools['send_message_to_user'](text, False)
    assert sent == [text]
    out = _asked(monkeypatch, text, where)
    assert out['refused'] == 'not_shown', out      # past the blank check
