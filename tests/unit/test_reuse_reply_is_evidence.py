"""#161: in REUSE a person's reply is the evidence that closes an ask step; it
never closes the step by itself, and the step waits for it instead of giving
up.

MEASURED 2026-10-30 22:24-22:28 IST on the installed desktop (reuse module =
HARTOS 19836e605), agent 45620673143 "Tutor-FirstSession-Setup": the first
turn delivered the ask ("what topic, how much detail"); the second turn's
answer "I'd like to focus on basic algebra, at a beginner level." got back
"I've sent you a message asking about your preferred topic and detail level.
I'm waiting for your response".  The verifier marked the ask step completed,
the receipt finder (_reuse_completion_evidence) wanted a receipt after the
dispatch, found none and recorded GAVE_UP.  The evidence kinds were
tool_receipt and user_visible_result only: nothing could say "the person
answered after the ask".

The contract (hartos-f9's reviews of 30 and 31 Oct):
  * an ASK is recorded per step, only for a step whose recipe flag reads
    'no' (autonomy_needs_user) and whose words name no tool other than
    send_message_to_user, when its send receipt or its written question sits
    in that step's dispatch window; at most one ask is open per step and the
    oldest open one is the one a reply answers;
  * the person's reply is captured when the reuse seed is built (text and
    request id); the seed says the question was already delivered and the
    words are the reply to judge, not to ask again; the evidence points at
    that seed: {'message_index', 'kind': 'user_reply', 'request_id',
    'reply', 'flow'};
  * the gate accepts user_reply only from a GENUINE user request
    (is_genuine_user_request: a daemon turn and an empty id are not), from a
    request OTHER than the one that asked (the 10-19 retry storm re-posts a
    request id), with a non-empty reply, inside the step's dispatch window;
  * the verifier's `completed` is still required: the finder only supplies
    the evidence; a refusal or an off-topic reply is the verifier's to judge;
  * the step's result is the person's words, never the seed; a user_reply is
    never promoted to the bridge and earns no success credit;
  * a 'no' step whose ask went out and has no reply yet WAITS through the
    canonical path (mark_action_waiting_for_user: ledger BLOCKED with
    blocked_reason input_required, ActionState PENDING), never GAVE_UP, and
    a genuine next turn resumes it as CREATE's does.

Driven through the real chat_agent, state machine, ledger and autogen group
chat, with only the model seats scripted (the shape of
tests/unit/test_reuse_role_handoff.py).
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import Flask

ASK_TEXT = 'send_message_to_user: ask Arjun to say ball and wait for his attempt'
ASK_LINE = 'Arjun, can you say ball for me?'
FINAL = 'Well done Arjun, ba is close to ball.'


@pytest.fixture
def speech(monkeypatch, tmp_path):
    from autogen import Agent, ConversableAgent, GroupChat, GroupChatManager
    from hartos import reuse_recipe as rr
    from hartos import lifecycle_hooks
    from integrations.agent_engine import budget_gate
    InMemoryBackend = pytest.importorskip('agent_ledger.backends').InMemoryBackend

    monkeypatch.chdir(tmp_path)
    created_keys = []
    for name in ('user_agents', 'role_agents', 'user_journey', 'user_tasks',
                 'user_ledgers', 'recipes', 'agent_data', 'llm_call_track',
                 'request_id_list', 'recent_file_id', '_reuse_resteer_counts',
                 '_reuse_fab_pending', '_reuse_pending_counts', '_reuse_pending_reply'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    monkeypatch.setattr(rr, 'create_schedule', Mock())
    monkeypatch.setattr(rr, 'send_message_to_user1', Mock())
    monkeypatch.setattr(budget_gate, 'charge_goal_work_completed', Mock())
    monkeypatch.setattr(rr, 'get_role', Mock(return_value='Executor'))

    def build(ask_action=None, ask_verdict='pending'):
        """`ask_verdict` is what the verifier says on the ASK turn, before any
        reply exists: 'pending' (the honest verdict) or 'completed' (the live
        4B's, which must no longer close the step)."""
        key = 'reply_evidence_test_61'
        user_id, prompt_id = 'reply_evidence_test', 61
        actions = [
            ask_action or {'action': ASK_TEXT, 'action_id': 1,
                           'can_perform_without_user_input': 'no',
                           'recipe': [{'steps': 'ask the child', 'tool_name': 'send_message_to_user'}]},
            {'action': 'praise Arjun for the attempt in the latest user message',
             'action_id': 2, 'can_perform_without_user_input': 'yes', 'recipe': []},
            {'action': 'say goodbye to Arjun', 'action_id': 3,
             'can_perform_without_user_input': 'yes', 'recipe': []},
        ]
        state = {'turn': 'ask', 'sends': [], 'read': [], 'dispatched': [], 'steers': []}

        def agent(name):
            return ConversableAgent(
                name, llm_config=False, human_input_mode='NEVER',
                code_execution_config=False, max_consecutive_auto_reply=20)

        assistant, helper, executor, verifier = map(
            agent, ('Assistant', 'Helper', 'Executor', 'StatusVerifier'))
        user, instructor = agent('User'), agent('ChatInstructor')

        def send_message_to_user(message: str) -> str:
            state['sends'].append(message)
            return 'Message sent'

        def read_item(item: int) -> str:
            state['read'].append(item)
            return f'Item {item}: observed value {item * 10}'

        executor.register_for_execution(name='send_message_to_user')(send_message_to_user)
        executor.register_for_execution(name='read_item')(read_item)
        assistant._hart_core_tools = [
            ('send_message_to_user', 'Send a message to the user', send_message_to_user),
            ('read_item', 'Read an item', read_item)]

        def assistant_reply(recipient, messages=None, sender=None, config=None):
            content = messages[-1].get('content') or ''
            if rr._REUSE_SYNTHESIS_ANSWER_SHAPE in content:
                return True, json.dumps({'message2userfinal': FINAL})
            aid = rr.user_tasks[key].current_action
            if aid == 1:
                if state['turn'] == 'ask' and not state['sends']:
                    return True, {'content': '', 'tool_calls': [{
                        'id': f'ask_{len(state["sends"])}', 'type': 'function',
                        'function': {'name': 'send_message_to_user',
                                     'arguments': json.dumps({'message': ASK_LINE})}}]}
                # The reply turn: the model judges, it does not ask again.
                return True, 'Arjun said "ba": a good attempt at ball.'
            if aid == 2:
                return True, 'Well done Arjun, ba is close to ball. Say ball once more!'
            return True, 'Bye Arjun, see you tomorrow.'

        def verdict_reply(recipient, messages=None, sender=None, config=None):
            aid = rr.user_tasks[key].current_action
            if aid == 1 and state['turn'] == 'ask':
                if ask_verdict == 'pending':
                    return True, json.dumps({'status': 'pending', 'action_id': 1,
                                             'message': "Awaiting the child's attempt",
                                             'can_perform_without_user_input': 'no'})
                return True, json.dumps({'status': 'completed', 'action_id': 1,
                                         'can_perform_without_user_input': 'no'})
            return True, json.dumps({'status': 'completed', 'action_id': aid})

        assistant.register_reply([Agent, None], assistant_reply)
        verifier.register_reply([Agent, None], verdict_reply)

        def select(last, group):
            if last in (user, instructor):
                content = group.messages[-1]['content']
                state['steers'].append(content)
                if rr._REUSE_ACTION_MESSAGE_PREFIX in content:
                    state['dispatched'].append(rr.user_tasks[key].current_action)
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
        ledger_box = {}

        def create(*args):
            rr.user_tasks[key] = rr.Action(actions)
            rr.recipes[key] = {'actions': actions}
            lifecycle_hooks.clear_action_states(key)
            ledger = rr.create_ledger_from_actions(
                user_id=user_id, prompt_id=prompt_id, actions=actions,
                backend=InMemoryBackend(), flow_id=0)
            rr.user_ledgers[key] = ledger
            lifecycle_hooks.register_ledger_for_session(key, ledger)
            rr.user_tasks[key].set_ledger(ledger)
            ledger_box['ledger'] = ledger
            created_keys.append(key)
            assert rr._start_reuse_action(key, 1, 'test start')
            return agents

        monkeypatch.setattr(rr, 'create_agents_for_user', Mock(side_effect=create))
        rr.user_agents[key] = create()
        rr.user_journey[key] = 'UseBot'
        rr.llm_call_track[key] = {'count': 0, 'original_prompt': False}

        def turn(text, request_id, kind='ask'):
            state['turn'] = kind
            return rr.chat_agent(user_id, text, prompt_id, None, request_id)

        return SimpleNamespace(rr=rr, L=lifecycle_hooks, key=key, state=state,
                               ledger=ledger_box['ledger'], turn=turn, gc=gc)

    with Flask(__name__).app_context():
        yield build

    for key in created_keys:
        lifecycle_hooks._ledger_registry.pop(key, None)
        lifecycle_hooks._groupchat_registry.pop(key, None)
        lifecycle_hooks.clear_action_states(key)


def _task(run, aid):
    return run.ledger.tasks[f'action_{aid}']


class TestTheAskTurnWaits:
    def test_a_pending_verdict_holds_the_step_in_the_canonical_wait(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(ask_verdict='pending')
        reply = run.turn('hi, I am ready', 'request-1')
        assert run.state['sends'] == [ASK_LINE]
        assert ASK_LINE in reply or 'ball' in reply.lower()
        assert run.rr.user_tasks[run.key].current_action == 1
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert _task(run, 1).blocked_reason == 'input_required'

    def test_a_completed_verdict_on_the_ask_turn_does_not_close_the_step(self, speech):
        """The live 4B's verdict (hartos-f9's case 2): today all three actions
        finish in turn 1 with no reply.  The flag alone does not pause; the
        recorded ask with no answer must."""
        from agent_ledger.core import TaskStatus
        run = speech(ask_verdict='completed')
        run.turn('hi, I am ready', 'request-1')
        assert run.state['sends'] == [ASK_LINE]
        assert run.rr.user_tasks[run.key].current_action == 1, (
            'the ask step closed on its own ask, and the flow ran on without the child')
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert _task(run, 2).status != TaskStatus.COMPLETED


class TestTheReplyTurnCompletesTheStep:
    def _asked(self, speech, ask_verdict='pending'):
        run = speech(ask_verdict=ask_verdict)
        run.turn('hi, I am ready', 'request-1')
        assert run.rr.user_tasks[run.key].current_action == 1
        return run

    @pytest.mark.parametrize('ask_verdict', ['pending', 'completed'])
    def test_a_genuine_reply_finishes_the_flow_without_asking_again(self, speech, ask_verdict):
        from agent_ledger.core import TaskStatus
        run = self._asked(speech, ask_verdict)
        with patch.object(run.L, '_promote_verified_outcome') as promote:
            reply = run.turn('ba', 'request-2', kind='reply')
        # The person gets the flow's last written step, not the question again.
        assert 'Bye Arjun' in reply and ASK_LINE not in reply, reply
        assert run.state['sends'] == [ASK_LINE], 'the person must not hear the question again'
        assert run.rr.user_tasks[run.key].current_action == 4
        assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        assert all(_task(run, a).status != TaskStatus.FAILED for a in (1, 2, 3))
        assert run.L.ledger_user_answered(run.key, 1, 0) is True
        # The reply is evidence for the step, never the agent's own output.
        assert 1 not in [c.args[1] for c in promote.call_args_list]
        assert any(c.args[1] in (2, 3) for c in promote.call_args_list)
        answers = _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY) or []
        assert answers and answers[-1]['answer'] == 'ba'

    def test_the_reply_seed_says_the_question_was_delivered(self, speech):
        run = self._asked(speech)
        ask_turn_steers = list(run.state['steers'])
        run.turn('ba', 'request-2', kind='reply')
        reply_turn_steers = run.state['steers'][len(ask_turn_steers):]
        seed = next(s for s in reply_turn_steers
                    if s.startswith(f'{run.rr._REUSE_ACTION_MESSAGE_PREFIX}1:'))
        assert run.rr._REUSE_REPLY_PREFACE in seed and seed.find('ba') > 0, seed
        assert not any(run.rr._REUSE_REPLY_PREFACE in s for s in ask_turn_steers), (
            'the ask turn seed is byte-identical to before')

    def test_a_retried_first_request_is_not_an_answer(self, speech):
        """The 10-19 web retry storm re-posts the same request id."""
        from agent_ledger.core import TaskStatus
        run = self._asked(speech)
        run.turn('hi, I am ready', 'request-1', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 1
        assert _task(run, 1).status != TaskStatus.COMPLETED
        assert run.L.ledger_user_answered(run.key, 1, 0) is False

    def test_a_daemon_turn_is_not_the_person(self, speech):
        from agent_ledger.core import TaskStatus
        run = self._asked(speech)
        run.turn('tick', 'daemon_goal_turn', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 1
        assert _task(run, 1).status != TaskStatus.COMPLETED
        assert run.L.ledger_user_answered(run.key, 1, 0) is False

    def test_an_empty_reply_holds(self, speech):
        from agent_ledger.core import TaskStatus
        run = self._asked(speech)
        run.turn('   ', 'request-2', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 1
        assert _task(run, 1).status != TaskStatus.COMPLETED


class TestANoStepNamingAnotherToolStillNeedsItsReceipt:
    def test_the_reply_alone_does_not_complete_a_tool_step(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(ask_action={'action': 'read_item 1, then confirm the value with the person',
                                 'action_id': 1, 'can_perform_without_user_input': 'no',
                                 'recipe': [{'steps': 'read', 'tool_name': 'read_item'}]})
        run.turn('go', 'request-1')
        run.turn('yes, confirmed', 'request-2', kind='reply')
        assert run.state['read'] == [], 'precondition: the tool never ran in this script'
        assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        assert run.rr.user_tasks[run.key].current_action == 1
