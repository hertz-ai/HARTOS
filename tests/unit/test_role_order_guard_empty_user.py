"""An empty user message is dropped before it reaches the model.

Measured on central 2026-09-13: the hosted Qwen endpoint answers any request
that holds a user message with empty content with a bare 400 "invalid
request". In a group chat another agent's turn reaches the speaker as
role=user, so an agent that said nothing arrives as {"role": "user",
"content": ""}. Compute Recruiter's recipe request opened with exactly that
(Message[0] user/Assistant "") and failed. Against the same endpoint the same
conversation passed once that message had text or was removed, and failed
with or without its name field. The ROLE-ORDER-GUARD already dropped the
empty assistant placeholder and let its user-side twin through.

Run:
  pytest tests/unit/test_role_order_guard_empty_user.py -q
"""
import unittest
from unittest.mock import MagicMock, patch

_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()

# The 11 messages StatusVerifier was handed at 13:28:17 on central, with the
# long contents shortened.  Roles, names and empty contents are as logged.
_RECIPE_TURN = [
    {'content': '', 'name': 'Assistant', 'role': 'user'},
    {'content': '{"status": "completed", "action_id": 1}',
     'role': 'assistant', 'name': 'StatusVerifier'},
    {'content': 'TERMINATE', 'name': 'ChatInstructor', 'role': 'user'},
    {'content': '[retry:recipe-1] Focus on the current task and create a recipe',
     'name': 'ChatInstructor', 'role': 'user'},
    {'content': '', 'role': 'assistant', 'name': 'StatusVerifier'},
    {'content': 'TERMINATE', 'name': 'ChatInstructor', 'role': 'user'},
    {'content': '@Assistant: To Get Action 1 fallback: Ask USER',
     'name': 'ChatInstructor', 'role': 'user'},
    {'content': '', 'name': 'Assistant', 'role': 'user'},
    {'content': '', 'role': 'assistant', 'name': 'StatusVerifier'},
    {'content': 'TERMINATE', 'name': 'ChatInstructor', 'role': 'user'},
    {'content': '[retry:recipe-1] Focus on the current task and create a recipe',
     'name': 'ChatInstructor', 'role': 'user'},
]


def _is_empty(msg):
    content = msg.get('content')
    return content is None or (isinstance(content, str) and not content.strip())


class EmptyUserMessageTests(unittest.TestCase):
    def setUp(self):
        _fake_current_app.logger.reset_mock()
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    def _guard(self, messages):
        return self.handler.validate_messages([dict(m) for m in messages])

    def test_the_recipe_turn_central_sent_reaches_the_model_without_an_empty_message(self):
        out = self._guard(_RECIPE_TURN)
        self.assertEqual([m for m in out if _is_empty(m)], [],
                         'an empty message still reaches the model, which answers 400')
        # Still a conversation the model can answer: StatusVerifier's status,
        # then the recipe request as the last word (the shape that got a 200).
        self.assertEqual([m['role'] for m in out], ['assistant', 'user'])
        self.assertTrue(out[-1]['content'].rstrip().endswith('create a recipe'))

    def test_the_guard_says_which_user_messages_it_dropped(self):
        self._guard(_RECIPE_TURN)
        lines = [c.args[0] for c in _fake_current_app.logger.info.call_args_list
                 if c.args and '[ROLE-ORDER-GUARD]' in str(c.args[0])]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('dropped 2 empty user message(s) at 0(Assistant), 7(Assistant)',
                      lines[0])
        self.assertIn('2 msgs out of 11 in', lines[0])

    def test_a_null_user_content_is_dropped_too(self):
        out = self._guard([{'role': 'user', 'content': None},
                           {'role': 'assistant', 'content': 'hi'},
                           {'role': 'user', 'content': 'go on'}])
        self.assertEqual([m['role'] for m in out], ['assistant', 'user'])

    def test_assistants_either_side_of_a_dropped_empty_message_still_merge(self):
        out = self._guard([{'role': 'user', 'content': 'plan it'},
                           {'role': 'assistant', 'content': 'step one'},
                           {'role': 'user', 'content': '   '},
                           {'role': 'assistant', 'content': 'step two'}])
        self.assertEqual([m['role'] for m in out], ['user', 'assistant'])
        self.assertIn('step one', out[1]['content'])
        self.assertIn('step two', out[1]['content'])

    def test_a_user_message_with_text_is_untouched(self):
        convo = [{'role': 'user', 'content': 'hello'},
                 {'role': 'assistant', 'content': 'hi'},
                 {'role': 'user', 'content': 'more please'}]
        self.assertEqual(self._guard(convo), convo)

    def test_an_empty_tool_result_is_kept(self):
        """A tool message is an answer slot keyed by tool_call_id; dropping an
        empty one would orphan its call."""
        out = self._guard([
            {'role': 'user', 'content': 'list files'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'ls', 'arguments': '{}'}}]},
            {'role': 'tool', 'content': '', 'tool_call_id': 'c1'},
        ])
        self.assertEqual([m['role'] for m in out], ['user', 'assistant', 'tool'])


if __name__ == '__main__':
    unittest.main()
