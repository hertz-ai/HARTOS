"""A turn held for another turn reads nothing, and says so.

hartos-17's review of ba153f51d (Medium).  A step waits for the person's
answer while another turn of the same build is running a tool.  The
person's answer arrives: get_response_group took it (_resume_prior_user_
input_block records the answer, releases the step's gate and the ledger's
wait) and only then met the hold, which answered held_for_another_turn_
reply(): "this message was not read. Please send the same message again".
In the speech demo the child is told to repeat an answer that was already
taken, and the step it was taken for is no longer held for them, so the
running turn goes on to the next step and the repeat is that step's answer.

A held turn now runs no part of the turn: both holds (the model binding and
another turn's running tool) come before the person's words are taken, so
the held reply is true.  The answer is recorded once, from the message they
send again, on the step that asked.  A message that was not read still says
so.

The second half: the hold's marker ("Held for another turn:") is for the
readers that hold the turn (the daemon and the hive worker read it in
/chat's reply), never for the person.  _chat_reply, the one way a /chat
reply leaves, takes it off a person's reply, so it is neither shown nor
spoken.

Real functions throughout: get_response_group, the gate, the ledger
(SmartLedger), the running-turn registry, _chat_reply's own source.  Only
the model round is stopped where its text is attached.

    python -m pytest tests/unit/test_a_held_turn_takes_no_answer.py --noconftest -q
"""
import ast
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hartos import create_recipe
from hartos.lifecycle_hooks import (
    ActionState, clear_action_states, safe_set_state,
)

_UP = 'held_turn_probe'
_SEND_AGAIN = 'send the same message again'


class _RoundStarted(Exception):
    pass


class AHeldTurnTakesNoAnswer(unittest.TestCase):

    def setUp(self):
        from agent_ledger.core import SmartLedger, add_actions_to_ledger, TaskStatus
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_s',
                                  ledger_dir=tempfile.mkdtemp())
        add_actions_to_ledger(self.ledger, [
            {'action': 'ask the child to say ball', 'action_id': 3}], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)
        self.ledger.tasks['action_3'].status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        # Step 3 asked the child in the turn that is still running.
        self.assertTrue(L.mark_action_waiting_for_user(
            _UP, 3, 'say ball', flow=0, asked_in='user-request-1'))
        create_recipe.user_tasks[_UP] = SimpleNamespace(
            current_action=3, _needs_user_input_action_id=3,
            _needs_user_input_kind='human_required', _needs_help_reason='say ball')

    def tearDown(self):
        clear_action_states(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
        create_recipe._TURNS_IN_FLIGHT.pop((_UP, create_recipe._CREATE_GROUP), None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list,
                      create_recipe.user_agents, create_recipe.messages):
            try:
                del store[_UP]
            except Exception:
                pass

    # -- the person's turns, whole, through get_response_group ------------

    def _held_turn(self, request, text, held_by):
        """A person's turn on the session's existing agents that is held:
        ``held_by`` 'tool', another thread is running this session's turn
        and its tool call is the group chat's last message (the reviewer's
        case); 'model', another turn runs on a different model (#172).
        Returns the reply; a round that starts fails the test."""
        from flask import Flask
        user_id, prompt_id = _UP.rsplit('_', 1)
        create_recipe.request_id_list[_UP] = request
        agents = tuple(MagicMock() for _ in range(7))
        agents[3].messages = []
        if held_by == 'tool':
            agents[3].messages = [
                {'role': 'user', 'content': 'Execute Action 3: ask the child to say ball'},
                {'role': 'assistant', 'content': 'Say ball with me.'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'c1', 'type': 'function',
                    'function': {'name': 'save_data_in_memory', 'arguments': '{}'}}]}]
            other = []
            t = threading.Thread(target=lambda: other.append(threading.get_ident()))
            t.start()
            t.join()
            create_recipe._TURNS_IN_FLIGHT[(_UP, create_recipe._CREATE_GROUP)] = {other[0]: 1}
        create_recipe.user_agents[_UP] = agents
        with ExitStack() as stack:
            stack.enter_context(Flask(__name__).app_context())
            stack.enter_context(patch.object(
                create_recipe, '_attach_for_create_turn',
                side_effect=AssertionError('a held turn ran its round')))
            stack.enter_context(patch.object(
                create_recipe.helper_fun, 'bind_agents_to_turn_model',
                return_value=held_by != 'model'))
            try:
                return create_recipe.get_response_group(user_id, text, prompt_id)
            finally:
                create_recipe.user_agents.pop(_UP, None)
                create_recipe._TURNS_IN_FLIGHT.pop(
                    (_UP, create_recipe._CREATE_GROUP), None)

    def _turn(self, request, text):
        """A person's turn that is not held.  Returns ('round', the text its
        model round is given) or ('reply', what it answered without one)."""
        from flask import Flask
        user_id, prompt_id = _UP.rsplit('_', 1)
        create_recipe.request_id_list[_UP] = request
        create_recipe.user_agents.pop(_UP, None)
        given = []

        def attach(agents_object, round_text, user_prompt):
            given.append(round_text)
            raise _RoundStarted()

        agents = tuple(MagicMock() for _ in range(7))
        agents[3].messages = []
        with ExitStack() as stack:
            stack.enter_context(Flask(__name__).app_context())
            stack.enter_context(patch.object(create_recipe, 'create_agents',
                                             return_value=agents))
            stack.enter_context(patch.object(create_recipe, '_attach_for_create_turn',
                                             side_effect=attach))
            try:
                reply = create_recipe.get_response_group(user_id, text, prompt_id)
            except _RoundStarted:
                return 'round', given[0]
            finally:
                create_recipe.user_agents.pop(_UP, None)
        return 'reply', reply

    # -- what the ledger and the gate hold ----------------------------------

    def _answers(self, aid=3):
        task = self.ledger.tasks.get(f'action_{aid}')
        if task is None:
            return []
        return [e['answer'] for e in
                task.context.get(self.L.USER_INPUT_EVIDENCE_KEY) or []]

    def _step_3_waits(self):
        task = create_recipe.user_tasks[_UP]
        return (task._needs_user_input_action_id == 3
                and create_recipe.person_waits_on(_UP, 3, 0))

    def _step_4_asks(self, asked_in='user-request-1'):
        """The build goes on to step 4, which asks the child something."""
        from agent_ledger.core import TaskStatus, add_actions_to_ledger
        add_actions_to_ledger(self.ledger, [
            {'action': 'ask which colour the ball is', 'action_id': 4}], flow_id=0)
        self.ledger.tasks['action_4'].status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 4, ActionState.IN_PROGRESS, 'working')
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 4, 'which colour', flow=0, asked_in=asked_in))
        task = create_recipe.user_tasks[_UP]
        task.current_action, task._needs_user_input_action_id = 4, 4
        task._needs_user_input_kind = 'human_required'

    def _the_running_turn_ends(self):
        """The turn that was running its tool ends.  Its loop goes on past
        step 3 only once nothing holds step 3 for the person (the gate and
        the ledger's wait); a step still held for them returns to them."""
        if not self._step_3_waits():
            self._step_4_asks(asked_in='user-request-1')

    # -- the tests ---------------------------------------------------------

    def test_a_held_answer_is_kept_nowhere_and_the_reply_says_so(self):
        """The reviewer's case, and the model binding's hold: the reply says
        the message was not read, so nothing of it may be kept, and the step
        still waits for the child."""
        from core.agent_tools import held_for_another_turn_reply
        for held_by in ('tool', 'model'):
            with self.subTest(held_by=held_by):
                self.tearDown()
                self.setUp()
                reply = self._held_turn('user-request-2', 'ba', held_by)
                self.assertEqual(reply, held_for_another_turn_reply())
                self.assertIn(_SEND_AGAIN, reply)
                self.assertEqual(
                    self._answers(), [],
                    "the child was told to send 'ba' again, and 'ba' was "
                    "already recorded as step 3's answer (review of ba153f51d)")
                self.assertTrue(self._step_3_waits(),
                                'a held turn released the step it never read')

    def test_no_reply_asks_again_for_an_answer_it_kept(self):
        """The walk the child makes: an answer met by the running tool, the
        same answer sent again as asked, the next step's question, the next
        message.  Every turn: a reply that asks for the message again kept
        nothing of it.  The answer is recorded once, on step 3, and the next
        message is step 4's answer, never step 3's a second time."""
        def turn(name, run):
            before = (self._answers(3), self._answers(4))
            said = run()
            if _SEND_AGAIN in str(said):
                self.assertEqual(
                    (self._answers(3), self._answers(4)), before,
                    f"the {name} turn kept the answer and asked for the "
                    f"message again: {said!r}")
            return said

        held = turn('held', lambda: self._held_turn('user-request-2', 'ba', 'tool'))
        self._the_running_turn_ends()
        if _SEND_AGAIN in str(held):
            turn('sent again', lambda: self._turn('user-request-3', 'ba'))
        self.assertEqual(self._answers(3), ['ba'], 'the answer is recorded once')
        if create_recipe.user_tasks[_UP]._needs_user_input_action_id != 4:
            self._step_4_asks(asked_in='user-request-3')
        turn('next', lambda: self._turn('user-request-4', 'red'))

        self.assertEqual(self._answers(3), ['ba'],
                         "the next message was taken as step 3's answer too")
        self.assertEqual(self._answers(4), ['red'])

    def test_the_message_sent_again_answers_this_step_not_the_next(self):
        """The reviewer's second half: told to send it again, the child does,
        and the step it was meant for must be the one that takes it.  Taken
        and released by the held turn, step 3 let the running turn go on to
        step 4, and the repeat became step 4's answer."""
        held = self._held_turn('user-request-2', 'ba', 'tool')
        self.assertIn(_SEND_AGAIN, held)
        self._the_running_turn_ends()
        kind, round_text = self._turn('user-request-3', 'ba')
        self.assertEqual((kind, round_text), ('round', 'ba'),
                         'the model never got the answer sent again')
        self.assertEqual(self._answers(4), [],
                         "the repeat of step 3's answer was taken as step 4's")
        self.assertEqual(self._answers(3), ['ba'])

    def test_a_held_message_with_no_step_waiting_still_says_it_was_not_read(self):
        """Guard: a held turn with nothing waiting for the person read
        nothing, and its reply still asks for the same message again."""
        from core.agent_tools import held_for_another_turn_reply
        task = create_recipe.user_tasks[_UP]
        self.assertEqual(self._turn('user-request-2', 'ba'), ('round', 'ba'))
        self.assertIsNone(task._needs_user_input_action_id)
        for held_by in ('tool', 'model'):
            with self.subTest(held_by=held_by):
                reply = self._held_turn('user-request-3', 'what time is it', held_by)
                self.assertEqual(reply, held_for_another_turn_reply())
                self.assertIn('was not read', reply)
                self.assertIn(_SEND_AGAIN, reply)
                self.assertEqual(self._answers(), ['ba'])


# -- the marker is for the readers that hold a turn, never for the person ---

def _chat_reply_alone():
    """hart_intelligence_entry._chat_reply from its own source (importing
    the module starts the whole backend), with what it reaches for stubbed:
    the speech engine, the memory, the cross-device mirror."""
    import integrations.agent_engine.dispatch  # noqa: F401  (real, before the stubs)
    import core.agent_tools  # noqa: F401
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'hart_intelligence_entry.py')
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == '_chat_reply']
    assert len(fns) == 1, f'expected one _chat_reply in {path}'
    mirror = MagicMock()
    pkg = MagicMock()
    pkg.chat_messages = mirror
    ns = {'_tts_synthesize_and_publish': MagicMock(),
          'get_memory': MagicMock(return_value=None),
          'app': MagicMock(),
          'jsonify': lambda payload: payload}
    modules = {'integrations.social': pkg,
               'integrations.social.chat_messages': mirror,
               'core.user_lang': MagicMock(get_preferred_lang=lambda: 'en'),
               'flask': MagicMock(has_request_context=lambda: False)}
    with patch.dict(sys.modules, modules):
        exec(compile(ast.Module(body=fns, type_ignores=[]), path, 'exec'), ns)
    return ns, mirror, modules


def _reply(request_id, text):
    """What a /chat reply carries for ``request_id``: the response the
    caller reads, the words spoken, the words mirrored to the person's
    other devices."""
    ns, mirror, modules = _chat_reply_alone()
    with patch.dict(sys.modules, modules):
        payload = ns['_chat_reply']('child-1', request_id, text)
    tts = ns['_tts_synthesize_and_publish']
    spoken = tts.call_args.args[0] if tts.call_args else None
    mirrored = [c.args[2] for c in mirror.persist_and_publish_async.call_args_list
                if c.args[1] == 'assistant']
    return SimpleNamespace(response=payload['response'], spoken=spoken,
                           mirrored=mirrored)


class TheHoldsMarkerNeverReachesThePerson(unittest.TestCase):

    def test_a_persons_held_reply_reads_and_is_spoken_without_the_marker(self):
        from core.agent_tools import held_for_another_turn_reply
        from core.constants import HELD_FOR_ANOTHER_TURN_REPLY_PREFIX
        r = _reply('user-request-7', held_for_another_turn_reply())
        for where, text in (('read', r.response), ('spoken', r.spoken),
                            ('mirrored', (r.mirrored or [None])[0])):
            with self.subTest(where=where):
                self.assertIsNotNone(text)
                self.assertNotIn(HELD_FOR_ANOTHER_TURN_REPLY_PREFIX, text)
                self.assertTrue(text.startswith('This conversation is still '
                                                'answering an earlier message'),
                                text)
                self.assertIn(_SEND_AGAIN, text)

    def test_a_background_turns_held_reply_keeps_it_for_the_daemon(self):
        """The daemon and the hive worker read the hold in /chat's reply
        (dispatch._turn_failure, worker_loop): a background turn's reply
        keeps the marker, so a held tick is still held, never a result."""
        from core.agent_tools import (held_for_another_turn_reply,
                                      is_held_for_another_turn)
        r = _reply('daemon_goal-1', held_for_another_turn_reply())
        self.assertEqual(r.response, held_for_another_turn_reply())
        self.assertTrue(is_held_for_another_turn(r.response))

    def test_any_other_reply_is_unchanged(self):
        r = _reply('user-request-7', 'Say ball with me.')
        self.assertEqual((r.response, r.spoken), ('Say ball with me.',) * 2)


if __name__ == '__main__':
    unittest.main()
