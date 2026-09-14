"""Role selection must enter the same replay loop as an existing session.

Run real AutoGen agents and the real replay/evidence code. Only model replies,
agent construction, scheduling and outward delivery are controlled boundaries.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask


@pytest.fixture
def replay(monkeypatch):
    from autogen import Agent, ConversableAgent, GroupChat, GroupChatManager
    from hartos import reuse_recipe as rr
    from hartos import lifecycle_hooks
    from integrations.agent_engine import budget_gate

    for name in ('user_agents', 'role_agents', 'user_journey', 'user_tasks',
                 'user_ledgers', 'recipes', 'agent_data', 'llm_call_track',
                 'request_id_list', 'recent_file_id', '_reuse_resteer_counts',
                 '_reuse_fab_pending', '_reuse_pending_counts'):
        monkeypatch.setattr(rr, name, {})
    schedule = Mock()
    monkeypatch.setattr(rr, 'create_schedule', schedule)
    monkeypatch.setattr(rr, 'send_message_to_user1', Mock())
    monkeypatch.setattr(budget_gate, 'charge_goal_work_completed', Mock())
    get_role = Mock(return_value='Reader')
    monkeypatch.setattr(rr, 'get_role', get_role)

    def build(prompt_id=42, journey='Roles', withhold_first=False):
        key = f'role_handoff_test_{prompt_id}'
        user_id = 'role_handoff_test'
        actions = [
            {'action': f'Read item {i}', 'action_id': i,
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': f'Read item {i}', 'tool_name': 'read_item'}]}
            for i in (1, 2)
        ]
        executed, dispatched, steers = [], [], []

        def agent(name):
            return ConversableAgent(
                name, llm_config=False, human_input_mode='NEVER',
                code_execution_config=False, max_consecutive_auto_reply=20)

        assistant, helper, executor, verifier = map(
            agent, ('Assistant', 'Helper', 'Executor', 'StatusVerifier'))
        user, instructor = agent('User'), agent('ChatInstructor')

        def read_item(item: int) -> str:
            executed.append(item)
            return f'Item {item}: observed value {item * 10}'

        executor.register_for_execution(name='read_item')(read_item)
        # Names are attachable to the real execution seat; the evidence gate
        # must see actual tool results before it credits either action.
        assistant._hart_core_tools = [('read_item', 'Read an item', read_item)]
        first_proposal = True

        def assistant_reply(recipient, messages=None, sender=None, config=None):
            nonlocal first_proposal
            content = messages[-1].get('content') or ''
            if rr._REUSE_SYNTHESIS_ANSWER_SHAPE in content:
                return True, json.dumps({'message2userfinal': 'Observed values: 10 and 20.'})
            aid = rr.user_tasks[key].current_action
            if withhold_first and first_proposal:
                first_proposal = False
                return True, 'The item is complete.'
            first_proposal = False
            return True, {'content': '', 'tool_calls': [{
                'id': f'item_{aid}_{len(executed)}', 'type': 'function',
                'function': {'name': 'read_item',
                             'arguments': json.dumps({'item': aid})}}]}

        def verdict_reply(recipient, messages=None, sender=None, config=None):
            return True, json.dumps({'status': 'completed',
                                     'action_id': rr.user_tasks[key].current_action})

        assistant.register_reply([Agent, None], assistant_reply)
        verifier.register_reply([Agent, None], verdict_reply)

        def select(last, group):
            if last in (user, instructor):
                content = group.messages[-1]['content']
                steers.append(content)
                if rr._REUSE_ACTION_MESSAGE_PREFIX in content:
                    dispatched.append(rr.user_tasks[key].current_action)
                return assistant
            if last is assistant:
                return executor if group.messages[-1].get('tool_calls') else verifier
            return verifier

        gc = GroupChat(agents=[assistant, helper, executor, verifier, user, instructor],
                       messages=[], max_round=8, speaker_selection_method=select,
                       allow_repeat_speaker=False)
        manager = GroupChatManager(gc, llm_config=False,
                                   is_termination_msg=rr._reuse_group_terminate)
        agents = (assistant, user, gc, manager, helper, None,
                  None, None, None, None, instructor, {})

        def create(*args):
            rr.user_tasks[key] = rr.Action(actions)
            rr.recipes[key] = {'actions': actions}
            lifecycle_hooks.clear_action_states(key)
            rr.safe_set_state(key, 1, rr.ActionState.ASSIGNED, 'test start')
            rr.safe_set_state(key, 1, rr.ActionState.IN_PROGRESS, 'test start')
            return agents

        create_main = Mock(side_effect=create)
        monkeypatch.setattr(rr, 'create_agents_for_user', create_main)
        role_chat = SimpleNamespace(messages=[])
        role_proxy = Mock()
        role_proxy.initiate_chat.side_effect = lambda *a, **k: role_chat.messages.append(
            {'name': 'assistant', 'role': 'assistant', 'content': 'TERMINATE'})
        roles = (None, role_proxy, role_chat, None, None, journey == 'single')
        monkeypatch.setattr(rr, 'create_agents_for_role', Mock(return_value=roles))
        if journey == 'Roles':
            rr.user_journey[key] = 'Roles'
            rr.role_agents[key] = roles
        elif journey == 'cached':
            rr.user_agents[key] = create()
            rr.user_journey[key] = 'UseBot'
            rr.llm_call_track[key] = {'count': 7, 'original_prompt': False}

        return SimpleNamespace(rr=rr, key=key, user_id=user_id, prompt_id=prompt_id,
                               executed=executed, dispatched=dispatched, steers=steers,
                               gc=gc, schedule=schedule, role_chat=role_chat,
                               role_proxy=role_proxy, create_main=create_main,
                               get_role=get_role, lifecycle=lifecycle_hooks)

    with Flask(__name__).app_context():
        yield build


@pytest.mark.parametrize('journey,prompt_id', [
    ('Roles', 42), ('Roles', '42'),
    ('Roles', '477c4c1d-7cb8-40b1-a9bc-22bb0d843105'),
    ('cached', 42), ('single', 42),
])
def test_replay_completes_and_delivers_answer(replay, journey, prompt_id):
    run = replay(prompt_id, journey)
    reply = run.rr.chat_agent(run.user_id, 'Reader; run the saved work',
                              prompt_id, 'file-7', 'request-8')

    assert reply == 'Observed values: 10 and 20.'
    assert run.executed == [1, 2]
    assert run.dispatched == [1, 2]
    assert 'Reader; run the saved work' in run.steers[0]
    assert run.steers[0].count(run.rr._REUSE_ACTION_MESSAGE_PREFIX) == 1
    assert run.rr.user_tasks[run.key].current_action == 3
    assert run.lifecycle.get_registered_groupchat(run.key) is run.gc
    assert run.rr.user_journey[run.key] == 'UseBot'
    assert run.schedule.call_count == (0 if journey == 'cached' else 1)
    assert run.create_main.call_count == (0 if journey == 'cached' else 1)
    assert run.rr.request_id_list[run.key] == 'request-8'
    assert run.rr.recent_file_id[run.user_id] == 'file-7'
    assert run.rr.llm_call_track[run.key] == {'count': 0, 'original_prompt': True}
    run.get_role.assert_called_with(run.user_id, prompt_id)


def test_selected_role_cannot_bypass_tool_evidence(replay):
    run = replay(withhold_first=True)
    reply = run.rr.chat_agent(run.user_id, 'Reader', run.prompt_id, None, 'request-9')

    assert reply == 'Observed values: 10 and 20.'
    assert run.executed == [1, 2]
    assert run.rr._reuse_resteer_counts[(run.key, 1)] == 1
    assert any(run.rr._REUSE_NOT_COMPLETE_MARKER in s for s in run.steers)


def test_unresolved_role_returns_question_without_starting_replay(replay):
    run = replay()
    run.role_proxy.initiate_chat.side_effect = lambda *a, **k: run.role_chat.messages.append(
        {'name': 'assistant', 'content': 'Which role would you like?'})
    assert run.rr.chat_agent(run.user_id, 'hello', run.prompt_id, None, 'request-10') == (
        'Which role would you like?')
    run.create_main.assert_not_called()
    run.schedule.assert_not_called()
    assert run.rr.user_journey[run.key] == 'Roles'
    assert run.executed == []
