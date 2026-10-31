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

    def build(ask_action=None, ask_verdict='pending', actions=None,
              assistant_script=None, verdict_script=None):
        """`ask_verdict` is what the verifier says on the ASK turn, before any
        reply exists: 'pending' (the honest verdict) or 'completed' (the live
        4B's, which must no longer close the step).  `actions` replaces the
        speech flow; `assistant_script(state, aid, content)` and
        `verdict_script(state, aid, content)` script the two model seats for
        other shapes and fall back to the speech script on None."""
        key = 'reply_evidence_test_61'
        user_id, prompt_id = 'reply_evidence_test', 61
        actions = actions or [
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
            if assistant_script is not None:
                scripted = assistant_script(state, aid, content)
                if scripted is not None:
                    return True, scripted
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
            if verdict_script is not None:
                scripted = verdict_script(state, aid, messages[-1].get('content') or '')
                if scripted is not None:
                    return True, scripted
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

        def cold(*args):
            """The cache-miss path over the session's existing Action and
            ledger (a process that still holds them): the production
            create_agents_for_user starts action 1 the same way."""
            rr._start_reuse_action(key, 1, 'reuse: first action')
            return agents

        def cold_turn(text, request_id, kind='ask'):
            rr.user_agents.pop(key, None)
            rr.create_agents_for_user.side_effect = cold
            return turn(text, request_id, kind)

        return SimpleNamespace(rr=rr, L=lifecycle_hooks, key=key, state=state,
                               ledger=ledger_box['ledger'], turn=turn, gc=gc,
                               cold_turn=cold_turn)

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
        # The person got the question through the send; the turn's reply is
        # the synthesis round's line after it (hartos-f9's item 6 on
        # b9edfd90a, carried: a second message after a send).  Pinned exactly
        # so a fix there shows.
        assert reply == FINAL, reply
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
        # The step's result is the person's words, never the seed they rode in.
        assert _task(run, 1).result == 'ba', _task(run, 1).result

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
        # Its send is not an ask either: the step is never held for a reply
        # (REUSE's branch choice, hartos-f9's mutant M1a).
        assert run.L.ledger_open_ask(run.key, 1, 0) is None
        assert _task(run, 1).status != TaskStatus.BLOCKED
        run.turn('yes, confirmed', 'request-2', kind='reply')
        assert run.state['read'] == [], 'precondition: the tool never ran in this script'
        assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        assert run.rr.user_tasks[run.key].current_action == 1

    def test_an_ask_on_a_tool_step_does_not_switch_off_the_fabrication_gate(self, speech, caplog):
        """hartos-00's review of c26ae7932, item 5: with an ask recorded by
        hand on a read_item step, the reply stood in for the send and the gate
        stopped re-steering.  No writer records such an ask today; the
        stand-in is limited to ask-only steps all the same."""
        import logging
        from agent_ledger.core import TaskStatus
        run = speech(ask_action={'action': 'read_item 1, then confirm the value with the person',
                                 'action_id': 1, 'can_perform_without_user_input': 'no',
                                 'recipe': [{'steps': 'read', 'tool_name': 'read_item'}]})
        run.turn('go', 'request-1')
        assert run.L.record_ask_delivered(run.key, 1, 'request-1', 0, 0, via='send')
        with caplog.at_level(logging.WARNING):
            run.turn('yes, confirmed', 'request-2', kind='reply')
        assert any('[FABRICATED-COMPLETE]' in r.getMessage() and 'action 1' in r.getMessage()
                   for r in caplog.records), 'the gate must still refuse the unrun read_item'
        assert run.state['read'] == []
        assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)

    def test_the_lifecycle_gate_itself_refuses_a_reply_for_a_tool_step(self, speech):
        """The gate's own scope rule, fed directly (hartos-f9's mutant M1b):
        a user_reply receipt for a step naming read_item is refused by
        _verifier_completion_has_conversation_evidence whatever REUSE's branch
        choice was; the same receipt for the ask-only step is accepted."""
        def cite(run, reply):
            messages = run.gc.messages
            ask_idx = max(i for i, m in enumerate(messages) if m.get('role') == 'tool')
            assert run.L.record_ask_delivered(run.key, 1, 'request-1', ask_idx, 0)
            messages.append({'role': 'user', 'name': 'User',
                             'content': run.rr._build_reuse_action_message(
                                 run.key, 1, user_words=reply)})
            evidence = {'message_index': len(messages) - 1, 'kind': 'user_reply',
                        'request_id': 'request-2', 'reply': reply, 'flow': 0}
            return run.L._verifier_completion_has_conversation_evidence(
                run.key, 1, {'evidence': evidence})

        tool_step = speech(ask_action={'action': 'read_item 1, then confirm the value with the person',
                                       'action_id': 1, 'can_perform_without_user_input': 'no',
                                       'recipe': [{'steps': 'read', 'tool_name': 'read_item'}]})
        tool_step.turn('go', 'request-1')
        assert cite(tool_step, 'yes, confirmed') is False
        ask_step = speech()
        ask_step.turn('hi, I am ready', 'request-1')
        assert cite(ask_step, 'ba') is True


# ─── hartos-f9's review of b9edfd90a: the four shapes it must not change ──────

PROSE_NO = {'action': 'Respond to the user with a short greeting', 'action_id': 1,
            'can_perform_without_user_input': 'no', 'recipe': []}
GREETING = 'Hello! Lovely to see you today.'
PLAN_FLOW = [
    {'action': "Tell the person today's plan", 'action_id': 1,
     'can_perform_without_user_input': 'no', 'recipe': []},
    {'action': 'note the plan in your own words', 'action_id': 2,
     'can_perform_without_user_input': 'yes', 'recipe': []},
    {'action': 'close with an encouraging line', 'action_id': 3,
     'can_perform_without_user_input': 'yes', 'recipe': []},
]
PROSE_OUT = {1: GREETING, 2: 'Noted: warm-up, then two sounds.', 3: 'You will do great today.'}


def _prose_assistant(state, aid, content):
    return PROSE_OUT.get(aid)


def _completed(state, aid, content):
    return json.dumps({'status': 'completed', 'action_id': aid})


class TestAProseNoStepThatOnlyWritesOutputCompletesAsBefore:
    """Critical 1: a 'no' step whose words name no tool and whose answer is
    its output ("Respond to user") must not hold on `completed`, and the next
    message must not become its answer.  Measured by hartos-f9 on b9edfd90a:
    2 of 5 turns complete, 0 promoted, the step's result was the person's
    next question."""

    def test_respond_to_user_completes_and_is_promoted_on_every_genuine_turn(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(actions=[PROSE_NO], assistant_script=_prose_assistant,
                     verdict_script=_completed)
        with patch.object(run.L, '_promote_verified_outcome') as promote:
            for n, words in enumerate(('hello', 'how are you', 'what can you do',
                                       'tell me a joke', 'thanks'), start=1):
                run.turn(words, f'request-{n}')
                assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED), (
                    f'turn {n}: {_task(run, 1).status} / {_task(run, 1).blocked_reason}')
                assert _task(run, 1).result == GREETING
        assert [c.args[1] for c in promote.call_args_list] == [1, 1, 1, 1, 1]
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        assert not _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY)
        assert run.L.ledger_open_ask(run.key, 1, 0) is None

    def test_a_prose_no_first_step_does_not_hold_and_the_next_message_is_not_its_answer(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(actions=PLAN_FLOW, assistant_script=_prose_assistant,
                     verdict_script=_completed)
        run.turn('ready', 'request-1')
        assert run.rr.user_tasks[run.key].current_action == 4
        assert all(_task(run, a).status != TaskStatus.BLOCKED for a in (1, 2, 3))
        run.turn('what is the weather like', 'request-2')
        assert not _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY)
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        assert run.L.ledger_open_ask(run.key, 1, 0) is None

    def test_a_written_question_on_a_pending_verdict_still_holds_and_the_reply_closes_it(self, speech):
        """The one shape where a written answer IS the ask: the verifier says
        the step waits (pending), the model wrote the question instead of
        sending it."""
        from agent_ledger.core import TaskStatus
        question = 'Which topic would you like today?'
        ask_prose = {'action': 'Ask the person which topic they want today', 'action_id': 1,
                     'can_perform_without_user_input': 'no', 'recipe': []}

        def assistant(state, aid, content):
            return question if state['turn'] == 'ask' else 'Algebra it is; a good choice.'

        def verifier(state, aid, content):
            status = 'pending' if state['turn'] == 'ask' else 'completed'
            return json.dumps({'status': status, 'action_id': aid})

        run = speech(actions=[ask_prose], assistant_script=assistant, verdict_script=verifier)
        reply = run.turn('hi', 'request-1')
        assert question in reply
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert _task(run, 1).blocked_reason == 'input_required'
        run.turn('algebra', 'request-2', kind='reply')
        assert _task(run, 1).status in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        answers = _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY) or []
        assert answers and answers[-1]['answer'] == 'algebra'


class TestADaemonTurnWalksTheParentsPaths:
    """Critical 2: until the owner rules on daemon replay of 'no' steps, a
    daemon turn takes exactly the parent's paths: the send receipt completes
    the step, it is promoted, no ask is recorded, nothing is held."""

    def test_a_daemon_tick_on_the_ask_step_finishes_the_flow_as_before(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(ask_verdict='completed')
        with patch.object(run.L, '_promote_verified_outcome') as promote:
            run.turn('tick', 'daemon_goal_turn')
        assert run.state['sends'] == [ASK_LINE]
        assert run.rr.user_tasks[run.key].current_action == 4
        assert 1 in [c.args[1] for c in promote.call_args_list]
        assert all(_task(run, a).status != TaskStatus.BLOCKED for a in (1, 2, 3))
        assert run.L.ledger_open_ask(run.key, 1, 0) is None

    def test_a_daemon_tick_with_a_pending_verdict_records_no_ask_and_holds_nothing(self, speech):
        from agent_ledger.core import TaskStatus
        run = speech(ask_verdict='pending')
        run.turn('tick', 'daemon_goal_turn')
        assert _task(run, 1).status != TaskStatus.BLOCKED
        assert run.L.ledger_open_ask(run.key, 1, 0) is None


class TestAQuestionWrittenInThePromptedFormatIsRecorded:
    """High 3: `@user {"message2userfinal": ...}` ends the round and the loop
    returns before any hold.  For a step whose words name send_message_to_user
    that written question is the ask; for a prose 'no' step naming no tool it
    is the step's output (hartos-f9's fix to the design)."""

    def test_an_ask_step_that_writes_its_question_holds_and_the_reply_closes_it(self, speech):
        from agent_ledger.core import TaskStatus

        def assistant(state, aid, content):
            if aid == 1 and state['turn'] == 'ask':
                return '@user ' + json.dumps({'message2userfinal': ASK_LINE})
            return None

        run = speech(assistant_script=assistant)
        reply = run.turn('hi, I am ready', 'request-1')
        assert ASK_LINE in reply
        assert run.state['sends'] == []
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert _task(run, 1).blocked_reason == 'input_required'
        assert run.L.ledger_open_ask(run.key, 1, 0) is not None
        ask_turn_steers = list(run.state['steers'])
        run.turn('ba', 'request-2', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 4
        assert run.L.ledger_user_answered(run.key, 1, 0) is True
        seed = next(s for s in run.state['steers'][len(ask_turn_steers):]
                    if s.startswith(f'{run.rr._REUSE_ACTION_MESSAGE_PREFIX}1:'))
        assert run.rr._REUSE_REPLY_PREFACE in seed

    def test_a_prose_no_step_ending_on_message2userfinal_is_not_an_ask(self, speech):
        from agent_ledger.core import TaskStatus

        def assistant(state, aid, content):
            return '@user ' + json.dumps({'message2userfinal': GREETING})

        run = speech(actions=[PROSE_NO], assistant_script=assistant)
        reply = run.turn('hello', 'request-1')
        assert GREETING in reply
        assert _task(run, 1).status != TaskStatus.BLOCKED
        assert run.L.ledger_open_ask(run.key, 1, 0) is None
        run.turn('what is the weather like', 'request-2')
        assert not _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY)
        assert run.L.ledger_user_answered(run.key, 1, 0) is False


class TestAHeldStepResumesFirstAndIsNeverRedispatchedInTheBackground:
    """High 4: the warm start used to start the step before looking at the
    hold, so the resume never ran and every tick asked again."""

    def test_the_reply_turn_resumes_the_held_step_through_the_one_edge(self, speech):
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        with patch.object(run.rr, 'resume_from_user_input',
                          wraps=run.rr.resume_from_user_input) as resume:
            run.turn('ba', 'request-2', kind='reply')
        assert resume.call_count == 1, resume.call_args_list
        assert resume.call_args.args[:2] == (run.key, 1)
        assert run.rr.user_tasks[run.key].current_action == 4

    def test_a_daemon_turn_on_a_held_step_neither_dispatches_nor_asks_again(self, speech):
        from agent_ledger.core import TaskStatus
        from core.agent_tools import is_waiting_for_person
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        dispatched, sends = list(run.state['dispatched']), list(run.state['sends'])
        reply = run.turn('tick', 'daemon_goal_turn', kind='reply')
        assert is_waiting_for_person(reply), reply
        assert run.state['dispatched'] == dispatched, 'the step was dispatched again'
        assert run.state['sends'] == sends, 'the person heard the question again'
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert run.rr.user_tasks[run.key].current_action == 1

    @pytest.mark.parametrize('request_id', [None, ''])
    def test_a_turn_with_no_request_id_gets_the_question_back(self, speech, request_id):
        """hartos-00's review of c26ae7932, item 4: a standalone /chat without
        a request id is not a dispatcher; it gets the pending question, never
        the internal sentence, and its words are never the answer (#46)."""
        from agent_ledger.core import TaskStatus
        from core.agent_tools import is_waiting_for_person
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        dispatched, sends = list(run.state['dispatched']), list(run.state['sends'])
        reply = run.turn('ba', request_id, kind='reply')
        assert reply == ASK_LINE, reply
        assert not is_waiting_for_person(reply)
        assert run.state['dispatched'] == dispatched
        assert run.state['sends'] == sends
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert run.L.ledger_user_answered(run.key, 1, 0) is False

    def test_the_cold_path_holds_a_held_step_too(self, speech):
        """Item 3: create_agents_for_user started action 1 before the second
        gate, moving BLOCKED to IN_PROGRESS, so a daemon tick after a cache
        miss re-dispatched the held step."""
        from agent_ledger.core import TaskStatus
        from core.agent_tools import is_waiting_for_person
        run = speech(ask_verdict='pending')
        run.turn('hi, I am ready', 'request-1')
        dispatched, sends = list(run.state['dispatched']), list(run.state['sends'])
        reply = run.cold_turn('tick', 'daemon_goal_turn', kind='reply')
        assert is_waiting_for_person(reply), reply
        assert run.state['dispatched'] == dispatched
        assert run.state['sends'] == sends
        assert _task(run, 1).status == TaskStatus.BLOCKED
        run.cold_turn('ba', 'request-2', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 4
        assert run.L.ledger_user_answered(run.key, 1, 0) is True
        assert run.state['sends'] == [ASK_LINE]

    def test_the_waiting_reply_is_neither_work_nor_a_strike_for_a_dispatcher(self, monkeypatch):
        """The daemon: dispatch_goal returns no response for it (the goal is
        not completed) and records a transient deferral (no backoff, no
        auto-pause).  The hive worker: held, not recorded, not released."""
        from core import agent_tools
        from integrations.agent_engine import dispatch
        from integrations.distributed_agent.worker_loop import (
            DistributedWorkerLoop, HeldForHelp)
        reply = agent_tools.waiting_for_person_reply(2)
        assert agent_tools.is_waiting_for_person(reply)
        assert not agent_tools.is_help_pause(reply) and not agent_tools.is_user_pause(reply)
        assert dispatch._failed_turn('g-wait', reply) is True
        assert dispatch.is_transient_deferral('g-wait') is True

        class _Task:
            task_id, parent_task_id, description = 'g_task_0', 'g', 'say ball'
            context = {'hop': 0, 'user_id': '42', 'prompt': 'say ball'}

        monkeypatch.setattr(DistributedWorkerLoop, '_dispatch_would_defer',
                            staticmethod(lambda: None))
        loop = DistributedWorkerLoop()
        with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                   return_value=(True, '', 'say ball')), \
             patch('integrations.agent_engine.dispatch.local_chat_dispatch',
                   return_value=('ok', reply)), \
             patch('integrations.agent_engine.dispatch.prompt_id_for_goal', return_value='p'):
            got = loop._execute_task(_Task())
        assert isinstance(got, HeldForHelp), got


CLAIM = 'I have sent Arjun a message asking him to say ball.'


def _ask_turn_then(reply_turn_verdict, ask_turn_text):
    """Scripts: on the ask turn the model writes ``ask_turn_text`` (no tool
    call) and the verifier says pending; on the reply turn the model writes
    a line and the verifier says ``reply_turn_verdict``."""
    def assistant(state, aid, content):
        if aid != 1:
            return None
        return ask_turn_text if state['turn'] == 'ask' else 'Arjun answered.'

    def verifier(state, aid, content):
        if aid != 1:
            return None
        status = 'pending' if state['turn'] == 'ask' else reply_turn_verdict
        return json.dumps({'status': status, 'action_id': 1,
                           'can_perform_without_user_input': 'no'})
    return assistant, verifier


class TestAClaimedSendIsNotAnAsk:
    """hartos-00's review of c26ae7932, item 1: the model wrote "I have sent
    Arjun a message ..." with no tool call, the verifier said pending, and the
    claim was recorded as the ask; "what message?" plus `completed` then
    completed the step with sends == [].  A written ask that the turn did not
    return never stands in for the send."""

    def test_the_probe_does_not_complete_the_step(self, speech):
        from agent_ledger.core import TaskStatus
        assistant, verifier = _ask_turn_then('completed', CLAIM)
        run = speech(assistant_script=assistant, verdict_script=verifier)
        run.turn('hi, I am ready', 'request-1')
        ask = run.L.ledger_open_ask(run.key, 1, 0)
        assert ask is not None and ask['via'] == 'written', ask
        with patch.object(run.L, '_promote_verified_outcome') as promote:
            run.turn('what message?', 'request-2', kind='reply')
        assert run.state['sends'] == []
        assert _task(run, 1).status not in (TaskStatus.COMPLETED, TaskStatus.TERMINATED)
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        assert run.rr.user_tasks[run.key].current_action == 1
        assert 1 not in [c.args[1] for c in promote.call_args_list]
        # The step gave up, so the ask it carried is abandoned and no later
        # message can answer it (item 2).
        assert run.L.ledger_open_ask(run.key, 1, 0) is None

    def test_a_returned_claim_followed_by_what_message_and_pending_holds(self, speech):
        """The half the code owns of hartos-00's note 1: a claim the turn
        RETURNED is recorded via 'reply' (the person saw it); "what message?"
        with `pending` holds and records nothing.  With `completed` it would
        close: that case relies on the verifier."""
        from agent_ledger.core import TaskStatus
        assistant, verifier = _ask_turn_then(
            'pending', '@user ' + json.dumps({'message2userfinal': CLAIM}))
        run = speech(assistant_script=assistant, verdict_script=verifier)
        run.turn('hi, I am ready', 'request-1')
        ask = run.L.ledger_open_ask(run.key, 1, 0)
        assert ask is not None and ask['via'] == 'reply' and ask['question'] == CLAIM, ask
        run.turn('what message?', 'request-2', kind='reply')
        assert run.state['sends'] == []
        assert _task(run, 1).status == TaskStatus.BLOCKED
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        assert run.rr.user_tasks[run.key].current_action == 1


class TestAFailedStepKeepsNoAsk:
    def test_a_step_that_gave_up_records_no_ask_from_its_last_words(self, speech):
        """Item 2: nothing was written on the ask turn; turn 2 re-steered to
        FAILED and the exit hook then recorded "Arjun replied." as a
        delivered ask, leaving a FAILED step with an open ask."""
        from agent_ledger.core import TaskStatus
        assistant, verifier = _ask_turn_then('completed', '')

        def reply_turn_says(state, aid, content):
            got = assistant(state, aid, content)
            return 'Arjun replied.' if (got is not None and state['turn'] != 'ask') else got

        run = speech(assistant_script=reply_turn_says, verdict_script=verifier)
        run.turn('hi, I am ready', 'request-1')
        assert run.L.ledger_open_ask(run.key, 1, 0) is None, 'nothing reached the person'
        run.turn('ba', 'request-2', kind='reply')
        assert _task(run, 1).status == TaskStatus.FAILED
        assert run.L.ledger_open_ask(run.key, 1, 0) is None


class TestTheOpenAskReaderReturnsTheOldest:
    def test_two_open_entries_read_oldest_first(self, speech):
        """ledger_open_ask's contract (hartos-f9's change 2): the OLDEST open
        ask is the one a reply answers.  record_ask_delivered opens no second
        ask while one is open, so the reader is fed two entries directly."""
        run = speech()
        run.turn('hi, I am ready', 'request-1')
        task = _task(run, 1)
        entries = list(task.context.get(run.L.ASK_DELIVERED_KEY) or [])
        assert len(entries) == 1 and entries[0]['request_id'] == 'request-1'
        newer = dict(entries[0], request_id='request-9', message_index=entries[0]['message_index'] + 1)
        assert run.ledger.update_task_context(task.task_id, {run.L.ASK_DELIVERED_KEY: entries + [newer]})
        assert run.L.ledger_open_ask(run.key, 1, 0)['request_id'] == 'request-1'


class TestAReplyJudgedInsufficientKeepsTheOneAsk:
    def test_the_second_ask_is_not_a_new_entry_and_the_next_reply_closes_it(self, speech):
        """hartos-f9's surviving mutant: oldest-vs-latest open ask.  Turn 2's
        reply is judged insufficient and the model asks again (a tool send);
        the ask entry stays the one from turn 1, and turn 3's reply closes it."""
        from agent_ledger.core import TaskStatus
        again = 'Say it once more, Arjun?'

        def assistant(state, aid, content):
            if aid != 1:
                return None
            if state['turn'] == 'ask' and not state['sends']:
                return None                      # the speech script sends ASK_LINE
            if state['turn'] == 'again':
                return {'content': '', 'tool_calls': [{
                    'id': 'ask_again', 'type': 'function',
                    'function': {'name': 'send_message_to_user',
                                 'arguments': json.dumps({'message': again})}}]}
            return None

        def verifier(state, aid, content):
            if aid == 1 and state['turn'] in ('ask', 'again'):
                return json.dumps({'status': 'pending', 'action_id': 1,
                                   'can_perform_without_user_input': 'no'})
            return None

        run = speech(assistant_script=assistant, verdict_script=verifier)
        run.turn('hi, I am ready', 'request-1')
        run.turn('ba', 'request-2', kind='again')
        assert _task(run, 1).status == TaskStatus.BLOCKED
        asks = _task(run, 1).context.get(run.L.ASK_DELIVERED_KEY) or []
        assert len(asks) == 1 and asks[0]['request_id'] == 'request-1'
        # A reply the verifier refused on `pending` records no answer, so the
        # ask stays open for the next one (the ask closes with the answer's
        # record, in one sealed save).
        assert asks[0]['answered'] is False
        assert run.L.ledger_user_answered(run.key, 1, 0) is False
        run.turn('ball', 'request-3', kind='reply')
        assert run.rr.user_tasks[run.key].current_action == 4
        assert run.state['sends'] == [ASK_LINE, again]
        asks = _task(run, 1).context.get(run.L.ASK_DELIVERED_KEY) or []
        assert len(asks) == 1 and asks[0]['answered'] is True
        answers = _task(run, 1).context.get(run.L.USER_INPUT_EVIDENCE_KEY) or []
        assert answers[-1]['answer'] == 'ball'
