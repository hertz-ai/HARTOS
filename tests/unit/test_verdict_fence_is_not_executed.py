"""A StatusVerifier verdict is JSON, never a code block the next turn executes.

LIVE, installed desktop on the 4B, 2026-10-06 02:35 -- the second learner turn
of REUSE agent 54 took 231 s where its neighbours took 10-30 s.  The first
turn's verifier had written its verdict as a fenced block

    ```json
    {"status": "completed", ...}
    ```

and when the learner's next message arrived, the Assistant -- whose
code_execution_config has no last_n_messages, so autogen scans every trailing
user message since the Assistant last spoke -- found the fence in its buffer
and RAN it:

    >>>>>>>> EXECUTING CODE BLOCK 0 (inferred language is json)...
    exitcode: 1 (execution failed)  Code output: unknown language json

state_transition read "exitcode:" and gave the turn back to the Assistant, ten
rounds at ~20 s each, until the loop's own guard ended it (task #163: the same
thing happens on every CREATE action).  The verifier's reply is now unwrapped
as it is sent (helper.unfence_verdict_before_send, registered by
helper.give_judge_view, the one place every verifier seat is configured), so
no seat ever holds a fenced verdict.

The behavioural tests run a REAL autogen group chat on a real code-executing
Assistant (scripted replies, no model), two turns on one chat with
clear_history=False, exactly the live shape.
"""
import ast
import os
import sys
import tempfile
import unittest

import autogen

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import helper  # noqa: E402

FENCE = chr(96) * 3
NL = chr(10)
VERDICT_JSON = ('{"status": "completed", "action": "Teach one step", '
                '"action_id": 1, "message": "The lesson was delivered."}')
FENCED_VERDICT = FENCE + 'json' + NL + VERDICT_JSON + NL + FENCE
LESSON = 'Plants make sugar from light. Check question: what do plants need?'
DISPATCH_1 = 'Perform this action -> Action #1:Teach one step. Teach me about plants.'
DISPATCH_2 = 'Perform this action -> Action #1:Teach one step. I think light.'


def _scripted(name, reply):
    seat = autogen.ConversableAgent(name, llm_config=False,
                                    human_input_mode='NEVER')
    seat.register_reply([autogen.Agent, None],
                        lambda recipient, messages, sender, config: (True, reply),
                        position=0)
    return seat


class _TwoTurns:
    """A real group chat: a code-executing Assistant and a verifier that
    writes ``verdict``, run twice on one chat without clearing its history."""

    def __init__(self, verdict, configure_verifier=None):
        self.assistant = autogen.AssistantAgent(
            'Assistant', llm_config=False,
            code_execution_config={'work_dir': tempfile.mkdtemp(),
                                   'use_docker': False})
        # The model's reply, reached only when nothing in the buffer is code:
        # appended AFTER autogen's own reply functions, which run first.
        self.assistant.register_reply(
            [autogen.Agent, None],
            lambda recipient, messages, sender, config: (True, LESSON),
            position=len(self.assistant._reply_func_list))
        self.verifier = _scripted('StatusVerifier', verdict)
        if configure_verifier is not None:
            configure_verifier(self.verifier)
        self.user = _scripted('User', 'TERMINATE')
        self.speakers = []
        self.group = autogen.GroupChat(
            agents=[self.user, self.assistant, self.verifier], messages=[],
            max_round=3,
            speaker_selection_method=lambda last, chat: self.speakers.pop(0))
        self.manager = autogen.GroupChatManager(groupchat=self.group,
                                                llm_config=False)

    def run(self):
        for dispatch, keep in ((DISPATCH_1, False), (DISPATCH_2, True)):
            self.speakers[:] = [self.assistant, self.verifier]
            self.user.initiate_chat(self.manager, message=dispatch,
                                    clear_history=not keep, silent=True)
        return self.group.messages

    def turn_two_reply(self):
        return [m for m in self.run() if m.get('name') == 'Assistant'][1]


class TestTheNextTurnDoesNotExecuteTheVerdict(unittest.TestCase):

    def test_the_assistants_second_turn_is_the_lesson(self):
        """THE LIVE FAILURE.  Without the hook this is 'exitcode: 1
        (execution failed) ... unknown language json'."""
        reply = _TwoTurns(FENCED_VERDICT,
                          helper.give_judge_view).turn_two_reply()
        self.assertEqual(reply['content'], LESSON)

    def test_no_message_in_the_log_is_an_execution_result(self):
        log = _TwoTurns(FENCED_VERDICT, helper.give_judge_view).run()
        self.assertEqual(
            [m for m in log if str(m.get('content')).startswith('exitcode:')],
            [])

    def test_the_log_holds_the_verdict_unfenced(self):
        log = _TwoTurns(FENCED_VERDICT, helper.give_judge_view).run()
        verdicts = [m['content'] for m in log
                    if m.get('name') == 'StatusVerifier']
        self.assertEqual(verdicts, [VERDICT_JSON, VERDICT_JSON])

    def test_a_verdict_that_was_never_fenced_is_left_as_it_is(self):
        log = _TwoTurns(VERDICT_JSON, helper.give_judge_view).run()
        self.assertEqual([m['content'] for m in log
                          if m.get('name') == 'StatusVerifier'],
                         [VERDICT_JSON, VERDICT_JSON])

    def test_the_control_without_the_hook_does_execute_it(self):
        """Proves the scenario can fail: a verifier no factory configured."""
        reply = _TwoTurns(FENCED_VERDICT).turn_two_reply()
        self.assertTrue(reply['content'].startswith('exitcode: 1'), reply)
        self.assertIn('unknown language json', reply['content'])


class TestTheHook(unittest.TestCase):
    def _hook(self, message):
        return helper.unfence_verdict_before_send(
            sender=None, message=message, recipient=None, silent=True)

    def test_a_fenced_json_object_is_unwrapped_in_a_dict_message(self):
        out = self._hook({'content': FENCED_VERDICT, 'role': 'assistant'})
        self.assertEqual(out, {'content': VERDICT_JSON, 'role': 'assistant'})

    def test_and_in_a_string_message(self):
        self.assertEqual(self._hook(FENCED_VERDICT), VERDICT_JSON)

    def test_a_fence_without_a_language_tag_is_unwrapped_too(self):
        self.assertEqual(
            self._hook(FENCE + NL + VERDICT_JSON + NL + FENCE), VERDICT_JSON)

    def test_text_around_the_fence_is_kept(self):
        message = 'Status:' + NL + FENCED_VERDICT + NL + 'Done.'
        self.assertEqual(self._hook(message),
                         'Status:' + NL + VERDICT_JSON + NL + 'Done.')

    def test_code_in_a_fence_is_left_alone(self):
        for body in ('print(1)', '[1, 2]'):
            message = FENCE + 'python' + NL + body + NL + FENCE
            self.assertEqual(self._hook(message), message)

    def test_a_message_with_no_fence_is_returned_as_it_came(self):
        message = {'content': VERDICT_JSON, 'role': 'assistant'}
        self.assertIs(self._hook(message), message)

    def test_content_that_is_not_text_is_not_touched(self):
        for message in ({'content': None, 'tool_calls': []},
                        {'content': [{'type': 'text'}]}, None, 7):
            self.assertEqual(self._hook(message), message)

    def test_the_input_message_is_not_mutated(self):
        message = {'content': FENCED_VERDICT, 'role': 'assistant'}
        self._hook(message)
        self.assertEqual(message['content'], FENCED_VERDICT)


def _calls(tree, func_name):
    """Every call of ``func_name`` (a bare name or an attribute) in ``tree``."""
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, 'id', None) == func_name
                 or getattr(n.func, 'attr', None) == func_name)]


def _builds_a_verifier(call):
    return any(kw.arg == 'name' and isinstance(kw.value, ast.Constant)
               and kw.value.value == 'StatusVerifier' for kw in call.keywords)


class TestEveryVerifierSeatGetsIt(unittest.TestCase):
    def test_give_judge_view_registers_the_hook(self):
        seat = _scripted('StatusVerifier', VERDICT_JSON)
        helper.give_judge_view(seat)
        self.assertIn(helper.unfence_verdict_before_send,
                      seat.hook_lists['process_message_before_send'])

    def test_source_guard_every_verifier_seat_is_configured_by_it(self):
        """The factories build the StatusVerifier seat five times (CREATE and
        REUSE, each for the main and the time-agent group, and the visual
        group); the hook rides on give_judge_view, so a function that builds
        one must call it.  test_verifier_sees_tool_activity_as_evidence pins
        the same call for the evidence view."""
        built = judged = 0
        for rel in ('create_recipe.py', 'reuse_recipe.py', 'helper.py'):
            tree = ast.parse(open(os.path.join(_ROOT, 'hartos', rel),
                                  encoding='utf-8').read())
            built += len([c for c in _calls(tree, 'AssistantAgent')
                          if _builds_a_verifier(c)])
            judged += len(_calls(tree, 'give_judge_view'))
        self.assertGreaterEqual(built, 4)
        self.assertGreaterEqual(judged, built)


if __name__ == '__main__':
    unittest.main()
