"""A REUSE turn held for another turn leaves the step waiting for the person.

The REUSE twin of 8b39a6197 (CREATE: a held turn takes no answer).  A
finished agent's step asked the child a question and waits for her answer
(ledger BLOCKED with input_required and an open ask).  Another turn of the
session's main group is running on another model, so the child's turn is
held: group_turn answers held_for_another_turn_reply(), "this message was not
read. Please send the same message again".  chat_agent ran the held-step gate
BEFORE that hold, and on a person's turn the gate resumes the step (BLOCKED
-> IN_PROGRESS).  The held turn read nothing, yet the step was no longer held
for the child: a background tick could then run it without her answer, and
the turn still running could move past it.

A held turn now leaves the step as it found it.  The step resumes only on a
turn that runs, so the child's message sent again is the answer, once, on
the step that asked.

Real functions throughout: chat_agent, the held-step gate, group_turn's
model hold and its running-turn registry, the state machine, the ledger and
an autogen group chat.  Only the model seats are scripted (the shape of
tests/unit/test_reuse_reply_is_evidence.py), and the running turn is a real
group_turn of the session's main group on another model, on its own thread.

    python -m pytest tests/unit/test_a_held_reuse_turn_keeps_the_step_waiting.py --noconftest -q
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

ASK_TEXT = 'send_message_to_user: ask Arjun to say ball and wait for his attempt'
ASK_LINE = 'Arjun, can you say ball for me?'
FINAL = 'Well done Arjun, ba is close to ball.'
DEFAULT = [{'model': 'free-model', 'api_key': 'k', 'base_url': 'http://127.0.0.1:9/v1'}]
EXPERT = [{'model': 'expert-model', 'api_key': 'k2', 'base_url': 'http://127.0.0.1:9/v2'}]
KEY, USER_ID, PROMPT_ID = 'held_reuse_turn_62', 'held_reuse_turn', 62
WAIT = 30


@pytest.fixture
def speech(monkeypatch, tmp_path):
    from autogen import Agent, ConversableAgent, GroupChat, GroupChatManager
    from hartos import reuse_recipe as rr
    from hartos import lifecycle_hooks
    from integrations.agent_engine import budget_gate
    InMemoryBackend = pytest.importorskip('agent_ledger.backends').InMemoryBackend

    monkeypatch.chdir(tmp_path)
    import core.cache_loaders as _cl
    monkeypatch.setattr(_cl, 'AGENT_DATA_DIR', str(tmp_path / 'agent_data'))
    for name in ('user_agents', 'role_agents', 'user_journey', 'user_tasks',
                 'user_ledgers', 'recipes', 'agent_data', 'llm_call_track',
                 'request_id_list', 'recent_file_id', '_reuse_resteer_counts',
                 '_reuse_fab_pending', '_reuse_pending_counts', '_reuse_pending_reply'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    monkeypatch.setattr(rr, 'config_list', list(DEFAULT))
    monkeypatch.setattr(rr, 'create_schedule', Mock())
    monkeypatch.setattr(rr, 'send_message_to_user1', Mock())
    monkeypatch.setattr(budget_gate, 'charge_goal_work_completed', Mock())
    monkeypatch.setattr(rr, 'get_role', Mock(return_value='Executor'))
    context = Flask(__name__).app_context()
    context.push()

    actions = [
        {'action': ASK_TEXT, 'action_id': 1, 'can_perform_without_user_input': 'no',
         'recipe': [{'steps': 'ask the child', 'tool_name': 'send_message_to_user'}]},
        {'action': 'praise Arjun for the attempt in the latest user message',
         'action_id': 2, 'can_perform_without_user_input': 'yes', 'recipe': []},
        {'action': 'say goodbye to Arjun', 'action_id': 3,
         'can_perform_without_user_input': 'yes', 'recipe': []},
    ]
    state = {'turn': 'ask', 'sends': [], 'calls': 0}

    def agent(name, llm_config=False):
        return ConversableAgent(
            name, llm_config=llm_config, human_input_mode='NEVER',
            code_execution_config=False, max_consecutive_auto_reply=20)

    assistant, executor, verifier = map(agent, ('Assistant', 'Executor', 'StatusVerifier'))
    # The one seat with a model, so the main group's agents are bound to a
    # turn's model (#172) and a turn on another model is held.  Never picked.
    helper = agent('Helper', {'config_list': list(DEFAULT), 'cache_seed': None})
    user, instructor = agent('User'), agent('ChatInstructor')

    def send_message_to_user(text: str, expects_reply: bool) -> str:
        state['sends'].append(text)
        return 'Message sent successfully to user with request_id: r'

    executor.register_for_execution(name='send_message_to_user')(send_message_to_user)
    assistant._hart_core_tools = [
        ('send_message_to_user', 'Send a message to the user', send_message_to_user)]

    def assistant_reply(recipient, messages=None, sender=None, config=None):
        state['calls'] += 1
        content = messages[-1].get('content') or ''
        if rr._REUSE_SYNTHESIS_ANSWER_SHAPE in content:
            return True, json.dumps({'message2userfinal': FINAL})
        aid = rr.user_tasks[KEY].current_action
        if aid == 1:
            if state['turn'] == 'ask' and not state['sends']:
                return True, {'content': '', 'tool_calls': [{
                    'id': 'ask_0', 'type': 'function',
                    'function': {'name': 'send_message_to_user',
                                 'arguments': json.dumps({'text': ASK_LINE,
                                                          'expects_reply': True})}}]}
            return True, 'Arjun said "ba": a good attempt at ball.'
        if aid == 2:
            return True, 'Well done Arjun, ba is close to ball. Say ball once more!'
        return True, 'Bye Arjun, see you tomorrow.'

    def verdict_reply(recipient, messages=None, sender=None, config=None):
        state['calls'] += 1
        aid = rr.user_tasks[KEY].current_action
        if aid == 1 and state['turn'] == 'ask':
            return True, json.dumps({'status': 'pending', 'action_id': 1,
                                     'message': "Awaiting the child's attempt",
                                     'can_perform_without_user_input': 'no'})
        return True, json.dumps({'status': 'completed', 'action_id': aid})

    assistant.register_reply([Agent, None], assistant_reply)
    verifier.register_reply([Agent, None], verdict_reply)

    def select(last, group):
        if last in (user, instructor):
            return assistant
        if last is assistant:
            return executor if group.messages[-1].get('tool_calls') else verifier
        return verifier

    gc = GroupChat(agents=[assistant, helper, executor, verifier, user, instructor],
                   messages=[], max_round=10, speaker_selection_method=select,
                   allow_repeat_speaker=False)
    manager = GroupChatManager(gc, llm_config=False,
                               is_termination_msg=rr._reuse_group_terminate)
    agents = (assistant, user, gc, manager, helper, None,
              None, None, None, None, instructor, {})

    rr.user_tasks[KEY] = rr.Action(actions)
    rr.recipes[KEY] = {'actions': actions}
    lifecycle_hooks.clear_action_states(KEY)
    ledger = rr.create_ledger_from_actions(
        user_id=USER_ID, prompt_id=PROMPT_ID, actions=actions,
        backend=InMemoryBackend(), flow_id=0)
    rr.user_ledgers[KEY] = ledger
    lifecycle_hooks.register_ledger_for_session(KEY, ledger)
    rr.user_tasks[KEY].set_ledger(ledger)
    assert rr._start_reuse_action(KEY, 1, 'test start')
    monkeypatch.setattr(rr, 'create_agents_for_user', Mock(return_value=agents))
    rr.user_agents[KEY] = agents
    rr.user_journey[KEY] = 'UseBot'
    rr.llm_call_track[KEY] = {'count': 0, 'original_prompt': False}

    def turn(text, request_id, kind='ask'):
        state['turn'] = kind
        return rr.chat_agent(USER_ID, text, PROMPT_ID, None, request_id)

    def another_turn_running():
        """A turn of the session's main group running on another model, on
        its own thread, through the one door chat_agent uses (group_turn).
        Returns (thread, release)."""
        from hartos import helper as helper_fun
        from hartos.threadlocal import thread_local_data
        entered, release, failed = threading.Event(), threading.Event(), []

        def run():
            thread_local_data.set_model_config_override(list(EXPERT))
            try:
                with helper_fun.group_turn((gc, manager), DEFAULT, KEY, 'reuse') as runs:
                    if not runs:
                        failed.append('the running turn was itself held')
                    entered.set()
                    release.wait(WAIT)
            finally:
                thread_local_data.clear_model_config_override()
                entered.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert entered.wait(WAIT) and not failed, failed
        return t, release

    try:
        yield SimpleNamespace(rr=rr, L=lifecycle_hooks, state=state, ledger=ledger,
                              gc=gc, turn=turn, another_turn_running=another_turn_running)
    finally:
        lifecycle_hooks._ledger_registry.pop(KEY, None)
        lifecycle_hooks._groupchat_registry.pop(KEY, None)
        lifecycle_hooks.clear_action_states(KEY)
        context.pop()


def _task(run, aid):
    return run.ledger.tasks[f'action_{aid}']


def _asked(run):
    """Turn 1: step 1 asks the child and waits for her answer."""
    from agent_ledger.core import TaskStatus
    assert run.turn('hi, I am ready', 'request-1') == ASK_LINE
    assert run.state['sends'] == [ASK_LINE]
    assert run.rr._reuse_held_action(KEY) == 1, 'precondition: step 1 waits for the child'
    assert _task(run, 1).status == TaskStatus.BLOCKED


def _held_turn(run, text, request_id):
    """The child's message while another turn of the session runs on another
    model.  Returns the reply and what the model seats and the log saw."""
    calls, logged = run.state['calls'], len(run.gc.messages)
    thread, release = run.another_turn_running()
    try:
        reply = run.turn(text, request_id, kind='reply')
    finally:
        release.set()
        thread.join(WAIT)
    return reply, run.state['calls'] - calls, len(run.gc.messages) - logged


def test_a_held_turn_leaves_the_step_waiting_and_takes_no_answer(speech):
    from agent_ledger.core import TaskStatus
    from core.agent_tools import is_held_for_another_turn
    run = speech
    _asked(run)
    reply, model_calls, logged = _held_turn(run, 'ba', 'request-2')

    assert is_held_for_another_turn(reply), reply
    assert model_calls == 0 and logged == 0, 'a held turn ran its round'
    # The step still waits for the child, exactly as before the held turn.
    assert run.rr._reuse_held_action(KEY) == 1, (
        'the held turn released the step waiting for the child '
        f'(ledger {_task(run, 1).status}, {_task(run, 1).blocked_reason})')
    assert _task(run, 1).status == TaskStatus.BLOCKED
    assert _task(run, 1).blocked_reason == 'input_required'
    assert run.rr.user_tasks[KEY].current_action == 1
    # Nothing of her message is kept: the reply that says so is true.
    assert run.L.recorded_answers(KEY, 1, 0) == []
    assert run.L.ledger_user_answered(KEY, 1, 0) is False
    assert (run.L.ledger_open_ask(KEY, 1, 0) or {}).get('request_id') == 'request-1'


def test_the_message_sent_again_is_the_answer_once_on_the_step_that_asked(speech):
    from agent_ledger.core import TaskStatus
    run = speech
    _asked(run)
    _held_turn(run, 'ba', 'request-2')

    reply = run.turn('ba', 'request-3', kind='reply')
    assert 'Bye Arjun' in reply and ASK_LINE not in reply, reply
    assert run.state['sends'] == [ASK_LINE], 'the child heard the question again'
    assert run.L.recorded_answers(KEY, 1, 0) == ['ba']
    assert _task(run, 1).result == 'ba', _task(run, 1).result
    assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
    assert run.L.recorded_answers(KEY, 2, 0) == [] and run.L.recorded_answers(KEY, 3, 0) == []


def test_a_background_tick_after_the_held_turn_neither_runs_nor_asks_again(speech):
    """What the release let through: with the step no longer held, the next
    tick of the agent's goal ran the step without the child's answer."""
    from core.agent_tools import is_waiting_for_person
    run = speech
    _asked(run)
    _held_turn(run, 'ba', 'request-2')

    calls = run.state['calls']
    reply = run.turn('tick', 'daemon_goal_tick', kind='tick')
    assert is_waiting_for_person(reply), reply
    assert run.state['calls'] == calls, 'the tick ran the step waiting for the child'
    assert run.state['sends'] == [ASK_LINE]
    assert run.rr._reuse_held_action(KEY) == 1
    assert run.L.recorded_answers(KEY, 1, 0) == []


def test_a_background_tick_on_the_held_step_never_enters_the_main_turn(speech,
                                                                       monkeypatch):
    """Review rv_66reuse of 00f20e6e6: the checks before the main group's
    turn refuse a background turn on a held step there, so it never takes
    the group's turn (group_turn's model binding and running-turn mark,
    which a person's turn would then be held behind).  The check inside the
    turn refuses it too, so a change that let the tick through the checks
    before it (``if not resume`` read before the person check) answered the
    same and passed every other test."""
    from contextlib import contextmanager
    from core.agent_tools import is_waiting_for_person
    from hartos import helper as helper_fun
    run = speech
    _asked(run)

    entered, real = [], helper_fun.group_turn

    @contextmanager
    def watched(agents, fallback_config_list, user_prompt, group):
        entered.append(group)
        with real(agents, fallback_config_list, user_prompt, group) as runs:
            yield runs
    monkeypatch.setattr(helper_fun, 'group_turn', watched)

    calls = run.state['calls']
    reply = run.turn('tick', 'daemon_goal_tick', kind='tick')
    assert is_waiting_for_person(reply), reply
    assert entered == [], f'the background tick entered group_turn: {entered}'
    assert run.state['calls'] == calls
    assert run.rr._reuse_held_action(KEY) == 1
    # The person's own reply still takes the main group's turn.
    assert run.turn('ba', 'request-2', kind='reply')
    assert entered and entered[0] == 'reuse', entered
