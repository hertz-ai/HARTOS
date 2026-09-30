"""Every request to a model carries a user turn, on the agent path too.

Measured on central 2026-09-13 (task #89): a tool-call turn reached the hosted
Qwen endpoint as [system, assistant, tool, assistant, tool] with no user
message and got a bare 400 "invalid request". Against the same endpoint,
splitting the bundled tool results alone still got the 400; adding a user turn
as well got a 200. The wire trim in core.llm_outbound_logger already seeded a
missing user turn, but it only intercepts local llama-server ports, so
central's hosted traffic never passed through it. ensure_user_turn is now the
one rule, applied by the wire trim and by ToolMessageHandler.validate_messages,
the last step of the agent path.

Run:
  pytest tests/unit/test_user_turn_is_guaranteed.py -q
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()


def _call(i, key):
    return {'id': 'call_%d' % i, 'type': 'function',
            'function': {'name': 'get_data_by_key',
                         'arguments': '{"key": "%s"}' % key}}


def _central_turn():
    """Helper's turn at 14:19:02 on central: no user message anywhere."""
    return [
        {'role': 'assistant', 'name': 'Helper', 'content': '\n\n',
         'tool_calls': [_call(1, 'hive_growth.signal_scan'),
                        _call(2, 'hive_growth.lead_pipeline')]},
        {'role': 'tool', 'name': 'Assistant', 'content': "{'scan_id': 'run1'}",
         'tool_responses': [
             {'tool_call_id': 'call_1', 'role': 'tool',
              'content': "{'scan_id': 'run1'}"},
             {'tool_call_id': 'call_2', 'role': 'tool',
              'content': "[{'lead_id': 'l1'}]"}]},
        {'role': 'assistant', 'content': '\n\n',
         'tool_calls': [_call(3, 'hive_growth.funnel')]},
        {'role': 'tool', 'name': 'Assistant', 'content': "{'funnel': []}"},
    ]


class EnsureUserTurnTests(unittest.TestCase):
    def setUp(self):
        from core.constants import WIRE_USER_SEED_TEXT
        from core.llm_outbound_logger import ensure_user_turn
        self.seed_text = WIRE_USER_SEED_TEXT
        self.ensure = ensure_user_turn

    def test_adds_one_right_after_the_system_message(self):
        msgs = [{'role': 'system', 'content': 's'},
                {'role': 'assistant', 'content': '', 'tool_calls': [_call(1, 'k')]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'r'}]
        self.assertTrue(self.ensure(msgs))
        self.assertEqual([m['role'] for m in msgs],
                         ['system', 'user', 'assistant', 'tool'])
        self.assertEqual(msgs[1]['content'], self.seed_text)

    def test_adds_one_at_the_front_without_a_system_message(self):
        msgs = [{'role': 'assistant', 'content': 'hi'}]
        self.assertTrue(self.ensure(msgs))
        self.assertEqual([m['role'] for m in msgs], ['user', 'assistant'])

    def test_a_conversation_with_a_user_turn_is_left_alone(self):
        msgs = [{'role': 'system', 'content': 's'},
                {'role': 'user', 'content': 'go'},
                {'role': 'assistant', 'content': 'ok'}]
        before = [dict(m) for m in msgs]
        self.assertFalse(self.ensure(msgs))
        self.assertEqual(msgs, before)

    def test_a_tool_result_is_not_a_user_turn(self):
        msgs = [{'role': 'tool', 'tool_call_id': 'c', 'content': 'r'}]
        self.assertTrue(self.ensure(msgs))
        self.assertEqual(msgs[0]['role'], 'user')

    def test_an_empty_conversation_is_not_given_one(self):
        msgs = []
        self.assertFalse(self.ensure(msgs))
        self.assertEqual(msgs, [])


class AgentPathTests(unittest.TestCase):
    def setUp(self):
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        from core.constants import WIRE_USER_SEED_TEXT
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)
        self.seed_text = WIRE_USER_SEED_TEXT

    def tearDown(self):
        self._patcher.stop()

    def test_the_central_tool_turn_leaves_the_handler_answerable(self):
        """The shape that got a 200: a user turn, then each call answered by
        its own tool message.  autogen puts the system message in front."""
        out = self.handler.apply_transform(_central_turn())
        self.assertEqual(out[0]['role'], 'user')
        self.assertEqual(out[0]['content'], self.seed_text)
        self.assertEqual([m for m in out if 'tool_responses' in m], [])
        self.assertEqual([m.get('tool_call_id') for m in out if m.get('role') == 'tool'],
                         ['call_1', 'call_2', 'call_3'])

    def test_dropping_the_only_user_message_is_followed_by_a_seed(self):
        """#86 drops an empty user message; when it was the only one, the
        conversation must not go out user-less."""
        out = self.handler.validate_messages([
            {'role': 'user', 'name': 'Assistant', 'content': ''},
            {'role': 'assistant', 'content': 'done'},
        ])
        self.assertEqual([m['role'] for m in out], ['user', 'assistant'])
        self.assertEqual(out[0]['content'], self.seed_text)

    def test_a_conversation_with_a_user_turn_is_not_seeded(self):
        convo = [{'role': 'user', 'content': 'hi'},
                 {'role': 'assistant', 'content': 'hello'}]
        self.assertEqual(self.handler.validate_messages([dict(m) for m in convo]),
                         convo)


if __name__ == '__main__':
    unittest.main()
