"""CREATE hands the action's own tool result to the StatusVerifier.

MEASURED live 2026-10-10 on the installed desktop (4B), agent 54 action 3,
"Call save_data_in_memory with the key teach.<that same user id> to save this
learner's updated progress ...", request a54-3c1e0febe271 (gui_app.log.1 and
gui_app.log): dispatched at 16:36:56, 16:38:23 and 16:39:54, given up at
16:41:12.  In those 4 minutes 16 seconds the model made 45 tool calls
(get_data_by_key 22, save_data_in_memory 7, get_user_id 5, read_book_page 4,
send_message_to_user 3, get_saved_metadata 3, list_books 1), answered in 42
tool messages.  Attempt 1 saved under keys it made up
(teach.user_11941619_11, teach.1234567890); the save the step asks for,
"Saved at teach.10202: {...}", came at 16:38:36 in attempt 2, and 28 tool
results followed it.  All 42 went back to the Assistant
(create_recipe.state_transition: "Message role is tool returning
assistant"), so the StatusVerifier, which speaks only after an Assistant turn
with no tool call, never spoke in any of the three attempts, and the build
ended on "[NEEDS-INPUT] action 3 not completing after 3 attempts", the
generic builder question, on a step whose work was done.

REUSE's selector already hands a tool result the Assistant ran to the
verifier (reuse_recipe.state_transition: ``return verify if last_speaker is
assistant else assistant``).  CREATE now does so for the result the
completion gate itself accepts as this action's receipt -- a real result of a
tool the action names, in its own dispatch window
(lifecycle_hooks.tool_result_is_action_work, which asks
_verifier_completion_has_conversation_evidence).  Any other tool result still
goes back to the Assistant: another tool's reply, a failed call, the
placeholder for a call that returned nothing, and every tool result of an
action that names no tool (that one is done by its written answer).

Driven through the REAL state_transition closure of create_agents (autogen's
constructors mocked, the seam tests/unit/test_create_gate_stops_the_chat.py
uses) and the REAL receipt gate, with the session's ledger and group chat
registered the way create_recipe registers them.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from core.constants import HISTORICAL_TOOL_PLACEHOLDER, tool_reply_failed  # noqa: E402
from hartos import create_recipe as cr  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import (  # noqa: E402
    clear_action_states, register_groupchat_for_session,
    register_ledger_for_session,
)

SAVE_ACTION = ("Call save_data_in_memory with the key teach.<that same user id> "
               "to save this learner's updated progress after the reply.")
PROSE_ACTION = "Write this turn's reply to the learner yourself, as your own chat message."
SERVED = ('save_data_in_memory', 'get_data_by_key', 'read_book_page',
          'send_message_to_user', 'get_user_id')


def _dispatch(action_id, text):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Execute Action {action_id}: {text} ,Latest User message: yes'}


def _call(call_id, fn, args):
    return {'role': 'assistant', 'name': 'Assistant', 'content': None,
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': fn, 'arguments': json.dumps(args)}}]}


def _result(call_id, body):
    """The aggregate shape autogen's tool executor writes to the group log."""
    return {'role': 'tool', 'name': 'Assistant', 'content': body,
            'tool_responses': [{'tool_call_id': call_id, 'role': 'tool',
                                'content': body}]}


def _named(*args, **kwargs):
    agent = MagicMock()
    agent.name = kwargs.get('name', 'agent')
    return agent


class _Ledger:
    def __init__(self, text):
        self.tasks = {'action_1': SimpleNamespace(description=text, context={})}


@pytest.fixture
def build(test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
    """The real CREATE selector for a one-action build, plus a way to put
    the session's ledger and group log in place for the receipt gate."""
    up = f'{test_user_id}_{test_prompt_id}'
    clear_action_states(up)
    cr._STATE_TRANSITION_LOOP_STATE.pop(up, None)
    cr._ASSISTANT_STREAK_STATE.pop(up, None)
    patches = [patch('hartos.create_recipe.config_list',
                     [{'model': 'test', 'api_key': 'test'}])]
    mocks = {}
    for name in ('AssistantAgent', 'UserProxyAgent', 'GroupChat', 'GroupChatManager'):
        p = patch(f'hartos.create_recipe.autogen.{name}', side_effect=_named)
        patches.append(p)
        mocks[name] = p.start()
    patches[0].start()

    def make(action_text, messages):
        task = Action([action_text])
        tasks = patch.object(cr, 'user_tasks', {up: task})
        tasks.start()
        patches.append(tasks)
        out = cr.create_agents(test_user_id, task, test_prompt_id)
        select = mocks['GroupChat'].call_args.kwargs['speaker_selection_method']
        agents = out[6]
        serving = SimpleNamespace(
            name='Helper', _function_map={t: (lambda: None) for t in SERVED},
            llm_config={'tools': []})
        group = SimpleNamespace(messages=messages, agents=[serving])
        # What create_agents registers is autogen's own GroupChat; the gate
        # reads the registered one, so the log under test is registered.
        register_groupchat_for_session(up, group)
        register_ledger_for_session(up, _Ledger(action_text))
        return SimpleNamespace(select=lambda: select(agents['assistant'], group),
                               assistant=agents['assistant'],
                               verify=agents['verify'])

    try:
        yield make
    finally:
        for p in reversed(patches):
            p.stop()
        register_groupchat_for_session(up, SimpleNamespace(messages=[], agents=[]))
        register_ledger_for_session(up, None)
        clear_action_states(up)
        cr._STATE_TRANSITION_LOOP_STATE.pop(up, None)
        cr._ASSISTANT_STREAK_STATE.pop(up, None)


SAVED = 'Saved at teach.10202: {"book": "The Water Cycle", "pages_taught": [1]}'


class TestTheActionsOwnWorkGoesToTheVerifier:

    def test_the_named_tools_real_result_is_judged_now(self, build):
        """The live shape of agent 54 action 3: the save succeeded."""
        s = build(SAVE_ACTION, [
            _dispatch(1, SAVE_ACTION),
            _call('c1', 'save_data_in_memory', {'key': 'teach.10202'}),
            _result('c1', SAVED)])
        assert s.select() is s.verify, (
            "the action's own save returned its work and went back to the "
            "Assistant, so the verifier never judged it: live 2026-10-10, 28 "
            "further tool results and [NEEDS-INPUT] after 3 attempts")

    def test_one_own_result_in_an_aggregate_is_enough(self, build):
        """Live 16:37:39 in that run (attempt 1): a send and a save answered
        in one tool message."""
        s = build(SAVE_ACTION, [
            _dispatch(1, SAVE_ACTION),
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [
                 {'id': 's1', 'type': 'function', 'function': {
                     'name': 'send_message_to_user', 'arguments': '{}'}},
                 {'id': 's2', 'type': 'function', 'function': {
                     'name': 'save_data_in_memory', 'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'Assistant', 'content': 'Message sent',
             'tool_responses': [
                 {'tool_call_id': 's1', 'role': 'tool', 'content': 'Message sent'},
                 {'tool_call_id': 's2', 'role': 'tool', 'content': SAVED}]}])
        assert s.select() is s.verify


READ_ACTION = ("Call get_user_id, then get_data_by_key with the key teach.<that "
               "user id> to read this learner's saved progress.")


class TestAnActionNamingTwoToolsWaitsForBoth:
    """Agent 54's action 1, verbatim in shape: the user id alone is not the
    progress the action reads."""

    def test_the_first_tools_result_goes_back_to_the_assistant(self, build):
        s = build(READ_ACTION, [
            _dispatch(1, READ_ACTION),
            _call('u1', 'get_user_id', {}),
            _result('u1', '10202')])
        assert s.select() is s.assistant, (
            'get_user_id alone handed the turn to the verifier; a "completed" '
            'there skips reading the saved progress')

    def test_the_second_tools_result_is_judged(self, build):
        s = build(READ_ACTION, [
            _dispatch(1, READ_ACTION),
            _call('u1', 'get_user_id', {}),
            _result('u1', '10202'),
            _call('k1', 'get_data_by_key', {'key': 'teach.10202'}),
            _result('k1', '[KV] teach.10202 = {"book": "The Water Cycle"}')])
        assert s.select() is s.verify

    def test_a_failed_second_call_does_not_count(self, build):
        s = build(READ_ACTION, [
            _dispatch(1, READ_ACTION),
            _call('u1', 'get_user_id', {}),
            _result('u1', '10202'),
            _call('k1', 'get_data_by_key', {'key': 'teach.10202'}),
            _result('k1', 'Error: the memory store is not readable'),
            _call('u2', 'get_user_id', {}),
            _result('u2', '10202')])
        assert s.select() is s.assistant


class TestEverythingElseStillGoesBackToTheAssistant:

    def test_another_tools_result(self, build):
        s = build(SAVE_ACTION, [
            _dispatch(1, SAVE_ACTION),
            _call('c1', 'read_book_page', {'page_number': 4}),
            _result('c1', '{"book": "The Water Cycle", "page_number": 1}')])
        assert s.select() is s.assistant

    def test_a_failed_call_of_the_named_tool(self, build):
        failed = 'Error: the memory store is not writable'
        assert tool_reply_failed(failed), 'precondition: the one failure rule'
        s = build(SAVE_ACTION, [
            _dispatch(1, SAVE_ACTION),
            _call('c1', 'save_data_in_memory', {'key': 'teach.10202'}),
            _result('c1', failed)])
        assert s.select() is s.assistant

    def test_the_placeholder_for_a_call_that_returned_nothing(self, build):
        s = build(SAVE_ACTION, [
            _dispatch(1, SAVE_ACTION),
            _call('c1', 'save_data_in_memory', {'key': 'teach.10202'}),
            _result('c1', HISTORICAL_TOOL_PLACEHOLDER)])
        assert s.select() is s.assistant

    def test_a_tool_result_of_an_action_that_names_no_tool(self, build):
        """A prose action is done by its written answer, not a tool reply.
        read_book_page is not bookkeeping, so the gate's receipt rule alone
        would accept its reply for an action naming no tool: only the
        names-no-tool rule keeps this result with the Assistant."""
        s = build(PROSE_ACTION, [
            _dispatch(1, PROSE_ACTION),
            _call('c1', 'read_book_page', {'page_number': 1}),
            _result('c1', '{"book": "The Water Cycle", "page_number": 1}')])
        assert s.select() is s.assistant

    def test_a_result_outside_the_actions_dispatch_window(self, build):
        """No dispatch of this action before it: not this action's receipt."""
        s = build(SAVE_ACTION, [
            _call('c1', 'save_data_in_memory', {'key': 'teach.10202'}),
            _result('c1', SAVED)])
        assert s.select() is s.assistant
