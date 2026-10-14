"""The CREATE chat stops at the first "needs the user" verdict, recovers from a
turn that ended on a tool call, and never shows a stub action as a dict.

MEASURED 2026-10-04, agent 20 (a gather-info salvage stub) / user 10202,
gui_app.log.1 17:10-17:16 and llm_outbound.jsonl.old (request
9b3ec1f8-...), 46 model calls:

  1. The stub's only action is the dict
         {'action': 'Respond to user', 'action_id': 1, 'status': 'pending'}
     (hart_intelligence_entry writes it at both salvage sites; 420 such
     configs were live), so every "Execute Action 1: ..." prompt and the
     help question the user finally read printed the dict verbatim:
         I need your input to finish building this agent. Step 1
         ("{'action': 'Respond to user', 'action_id': 1, 'status': 'pending'}")
  2. The StatusVerifier answered {"status": "pending",
     "can_perform_without_user_input": "no"} six times in the first turn.
     state_transition's USER-INPUT-GATE set the sticky flag each time and
     then RETURNED THE ASSISTANT, so the inner autogen chat ran on to its
     max_round=30 cap; each re-execution of "Respond to user" called
     send_message_to_user, which is the three greeting bubbles the user saw.
  3. Turn 2 hit the cap right after the model issued save_data_in_memory
     (17:16:36.515).  That unanswered tool call stayed the last group-chat
     message, and get_response_group's guard answered 'Processing a tool
     now please try later' to EVERY later message (four voice turns
     18:28-18:31), forever: the state is process memory.

Three contracts, each driven through the REAL code:
  - the gate ENDS the inner chat (state_transition returns None, the way the
    loop-break and the TERMINATE guard already do) once it flags an action;
  - a tool call orphaned by a turn that ended is settled (a tool message per
    unanswered id, carrying the historical placeholder so no reader takes
    it for a real result) and the turn runs; the canned line is kept only
    while ANOTHER thread is running this session's turn, the one case it was
    written for;
  - a salvage-stub action reads as its text through the one CREATE-side
    constructor, create_action_with_ledger, so prompts and the help question
    say "Respond to user".
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from core.constants import HISTORICAL_TOOL_PLACEHOLDER  # noqa: E402
from hartos import create_recipe as cr  # noqa: E402
from hartos import helper as h  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import (  # noqa: E402
    ActionState, clear_action_states, get_action_state,
)
from hartos.threadlocal import thread_local_data  # noqa: E402

STUB = {'action': 'Respond to user', 'action_id': 1, 'status': 'pending'}
CANNED = 'Processing a tool now please try later'
POSTED = {'role': 'user', 'name': 'ChatInstructor',
          'content': 'Execute Action 1: Respond to user ,Latest User message: '
                     'Run a thought experiment'}


def _verdict(gate):
    """The StatusVerifier's live verdict shape, with the gate value under test."""
    return {'role': 'user', 'name': 'StatusVerifier', 'content': json.dumps({
        'status': 'pending', 'action': 'Respond to user', 'action_id': 1,
        'message': 'the person has to say what the experiment is about',
        'can_perform_without_user_input': gate})}


def _named(*args, **kwargs):
    """A distinct agent per constructor call, carrying the name create_agents
    gave it, so the selector's last_speaker.name comparisons are real."""
    agent = MagicMock()
    agent.name = kwargs.get('name', 'agent')
    return agent


@pytest.fixture
def selector(test_user_id, test_prompt_id, mock_flask_app, sample_config_json):
    """The REAL state_transition closure of create_agents, with autogen's
    constructors mocked (the same seam tests/unit/test_agent_creation.py
    uses), bound to a one-action task like agent 20's."""
    up = f'{test_user_id}_{test_prompt_id}'
    clear_action_states(up)
    cr._STATE_TRANSITION_LOOP_STATE.pop(up, None)
    task = Action(['Respond to user'])
    patches = [
        patch.object(cr, 'user_tasks', {up: task}),
        patch('hartos.create_recipe.config_list',
              [{'model': 'test', 'api_key': 'test'}]),
    ]
    mocks = {}
    for name in ('AssistantAgent', 'UserProxyAgent', 'GroupChat', 'GroupChatManager'):
        p = patch(f'hartos.create_recipe.autogen.{name}', side_effect=_named)
        patches.append(p)
        mocks[name] = p.start()
    for p in patches[:2]:
        p.start()
    try:
        out = cr.create_agents(test_user_id, task, test_prompt_id)
        select = mocks['GroupChat'].call_args.kwargs['speaker_selection_method']
        agents_object = out[6]
        yield SimpleNamespace(up=up, task=task, select=select,
                              assistant=agents_object['assistant'],
                              verify=agents_object['verify'])
    finally:
        for p in reversed(patches):
            p.stop()
        clear_action_states(up)
        cr._STATE_TRANSITION_LOOP_STATE.pop(up, None)


class TestTheGateEndsTheInnerChat:

    def test_a_flagged_verdict_ends_the_chat_at_once(self, selector):
        s = selector
        assert get_action_state(s.up, 1) != ActionState.TERMINATED
        group = SimpleNamespace(messages=[POSTED, _verdict('no')], agents=[])
        chosen = s.select(s.verify, group)
        assert s.task._needs_user_input_action_id == 1, 'the gate did not flag'
        assert chosen is None, (
            'the gate flagged action 1 but handed the turn back to the '
            'Assistant: autogen then runs to max_round (30 rounds, three '
            'greeting bubbles, live 2026-10-04 17:10)')

    def test_an_unflagged_pending_verdict_still_routes_to_the_assistant(self, selector):
        s = selector
        group = SimpleNamespace(messages=[POSTED, _verdict('yes')], agents=[])
        chosen = s.select(s.verify, group)
        assert getattr(s.task, '_needs_user_input_action_id', None) is None
        assert chosen is s.assistant


@pytest.fixture
def session(test_user_id, test_prompt_id, mock_flask_app):
    """A cached CREATE session whose last group-chat message is an unanswered
    tool call: what agent 20's session looked like from 17:16:36 on."""
    up = f'{test_user_id}_{test_prompt_id}'
    clear_action_states(up)
    task = Action(['Respond to user'])
    task._needs_user_input_action_id = 1
    task._needs_user_input_kind = 'human_required'
    orphan = {'role': 'assistant', 'name': 'Helper', 'content': None,
              'tool_calls': [{'id': 'call_memo_1', 'type': 'function',
                              'function': {'name': 'save_data_in_memory',
                                           'arguments': '{"key": "thought_experiment_concept"}'}}]}
    group_chat = SimpleNamespace(messages=[
        POSTED,
        {'role': 'user', 'name': 'Assistant',
         'content': 'Could you say what the experiment is about?'},
        orphan,
    ])
    agents_object = {k: MagicMock() for k in ('user', 'helper', 'assistant', 'verify', 'executor')}
    chat_instructor = MagicMock()
    chat_instructor.chat_messages = {}
    cached = (MagicMock(), agents_object['assistant'], agents_object['executor'],
              group_chat, MagicMock(), chat_instructor, agents_object)
    patches = [
        patch.object(cr, 'user_tasks', {up: task}),
        patch.object(cr, 'user_agents', {up: cached}),
        patch.object(cr, 'messages', {up: [{'role': 'user', 'content': 'Run a thought experiment'}]}),
        patch.object(cr, 'request_id_list', {up: 'req-live-1'}),
        patch.object(cr, '_resume_prior_user_input_block', return_value=False),
        patch.object(cr, '_attach_for_create_turn'),
    ]
    for p in patches:
        p.start()
    previous_rid = thread_local_data.get_request_id()
    thread_local_data.set_request_id('req-live-1')
    try:
        yield SimpleNamespace(up=up, task=task, group_chat=group_chat,
                              agents_object=agents_object, orphan=orphan,
                              user_id=test_user_id, prompt_id=test_prompt_id)
    finally:
        thread_local_data.set_request_id(previous_rid or '')
        for p in reversed(patches):
            p.stop()
        clear_action_states(up)


class TestAnOrphanedToolCallIsSettled:

    def test_the_orphan_is_closed_and_the_turn_runs(self, session):
        s = session
        reply = cr.get_response_group(s.user_id, 'ok, a philosophical one', s.prompt_id)
        assert reply != CANNED, (
            "the session answered the canned line to a message with no turn "
            "in flight: that is the 'Processing a tool now please try later' "
            "forever of 2026-10-04 18:28-18:31")
        s.agents_object['user'].initiate_chat.assert_called_once()
        assert not cr.has_pending_tool_calls(s.group_chat.messages)
        settled = s.group_chat.messages[-1]
        assert settled['role'] == 'tool'
        assert h.answered_call_ids(settled) == {'call_memo_1'}
        # The historical placeholder, the one vocabulary every reader knows
        # means "no real result": the fabrication gate must not take the
        # settle for the tool having run.
        assert settled['content'] == HISTORICAL_TOOL_PLACEHOLDER
        # The orphan itself stays in the record; only its answer slot is filled.
        assert s.orphan in s.group_chat.messages
        # Still blocked on the user, so the turn asks (with the TEXT, no dict).
        assert reply == cr._needs_input_reply(1, 'Respond to user')

    def test_the_canned_line_is_kept_while_another_thread_runs_this_session(self, session):
        s = session
        other = []
        t = threading.Thread(target=lambda: other.append(threading.get_ident()))
        t.start()
        t.join()
        cr._TURNS_IN_FLIGHT[s.up] = {other[0]: 1}
        try:
            reply = cr.get_response_group(s.user_id, 'and again', s.prompt_id)
        finally:
            cr._TURNS_IN_FLIGHT.pop(s.up, None)
        assert reply == CANNED
        s.agents_object['user'].initiate_chat.assert_not_called()
        assert cr.has_pending_tool_calls(s.group_chat.messages), \
            'a turn in flight owns that tool call; nothing may settle it'


def test_the_turn_marker_is_per_session_and_re_entrant():
    """recipe() marks the thread running a session's turn.  get_response_group
    re-enters itself (safe_action_boundary_check, the flow-increment sites),
    so the same thread entering again is still the one turn, and only a
    DIFFERENT thread counts as a turn in flight."""
    up = 'marker_probe_session'
    me = threading.get_ident()
    assert not cr._another_turn_in_flight(up)
    with cr._turn_in_flight(up):
        assert not cr._another_turn_in_flight(up)
        with cr._turn_in_flight(up):
            assert cr._TURNS_IN_FLIGHT[up] == {me: 2}
        assert cr._TURNS_IN_FLIGHT[up] == {me: 1}, \
            'the inner exit must not clear the outer turn'
        seen = []
        t = threading.Thread(target=lambda: seen.append(cr._another_turn_in_flight(up)))
        t.start()
        t.join()
        assert seen == [True]
    assert up not in cr._TURNS_IN_FLIGHT


def test_a_turn_that_ends_does_not_unmark_a_turn_still_running():
    """Review of ff160f057 (2026-10-05): with ONE owner per session, three
    overlapping /chat requests emptied the table when the first returned, and
    the third request then settled the second's live tool call.  Every running
    thread is counted on its own."""
    up = 'marker_overlap_probe'
    first_in, first_may_leave, first_left = (threading.Event(), threading.Event(),
                                             threading.Event())
    second_in, second_may_leave = threading.Event(), threading.Event()

    def first():
        with cr._turn_in_flight(up):
            first_in.set()
            first_may_leave.wait(10)
        first_left.set()

    def second():
        with cr._turn_in_flight(up):
            second_in.set()
            second_may_leave.wait(10)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    assert first_in.wait(10)
    t2.start()
    assert second_in.wait(10)
    first_may_leave.set()
    assert first_left.wait(10)
    try:
        # This thread is the third request: the second turn is still running.
        assert cr._another_turn_in_flight(up), (
            'the first turn ending unmarked the second, still running')
    finally:
        second_may_leave.set()
        t1.join(10)
        t2.join(10)
    assert not cr._another_turn_in_flight(up)
    assert up not in cr._TURNS_IN_FLIGHT


def test_the_settle_reaches_every_agents_own_history(session):
    """The model reads each agent's own history, not the group list, and there
    the orphaned call was still the newest assistant tool call, which
    ToolMessageHandler leaves alone as 'active'.  The settle goes where
    run_chat puts a reply: through the manager to every agent."""
    s = session
    a1, a2 = MagicMock(), MagicMock()
    a1.name, a2.name = 'Assistant', 'Helper'
    s.group_chat.agents = [a1, a2]
    manager = cr.user_agents[s.up][4]
    cr.get_response_group(s.user_id, 'ok, a philosophical one', s.prompt_id)
    sent_to = [c.args[1] for c in manager.send.call_args_list]
    assert sent_to == [a1, a2], manager.send.call_args_list
    for c in manager.send.call_args_list:
        msg = c.args[0]
        assert msg['role'] == 'tool'
        assert h.answered_call_ids(msg) == {'call_memo_1'}
        assert c.kwargs == {'request_reply': False, 'silent': True}


class TestAStubActionReadsAsItsText:

    @staticmethod
    def _build(actions, up):
        from agent_ledger.backends import InMemoryBackend
        with patch.object(cr, 'get_production_backend', lambda *a, **k: InMemoryBackend()), \
                patch.object(cr, 'user_ledgers', {}), \
                patch.object(cr, 'user_delegation_bridges', {}):
            return cr.create_action_with_ledger(actions, 7, 20, up, flow_id=0)

    def test_the_salvage_dict_becomes_its_text(self, mock_flask_app):
        task = self._build([dict(STUB)], 'stub_text_probe_1')
        assert task.get_action(0) == 'Respond to user'
        assert task.ledger.tasks['action_1'].description == 'Respond to user'
        assert cr._needs_input_reply(1, task.get_action(0)) == (
            cr._needs_input_reply(1, 'Respond to user'))
        assert "{'action'" not in cr._needs_input_reply(1, task.get_action(0))

    def test_other_shapes_are_left_alone(self, mock_flask_app):
        rich = {'action': 'Search the web for the three sources',
                'action_id': 1, 'status': 'pending', 'tool_name': 'google_search'}
        task = self._build(['Plain text action', rich], 'stub_text_probe_2')
        assert task.get_action(0) == 'Plain text action'
        assert task.get_action(1) is rich
