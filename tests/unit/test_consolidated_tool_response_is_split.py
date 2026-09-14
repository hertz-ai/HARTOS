"""A bundled tool reply reaches the model as one tool message per call.

Measured on central 2026-09-13 14:19:03 (task #89): Helper made two
get_data_by_key calls in one turn, and autogen returned both results as ONE
role=tool message, {"tool_responses": [...]}, with no top-level tool_call_id.
ToolMessageHandler.apply_transform recognised it as a consolidated response,
found its assistant, and inserted it back as that single message, so the
hosted Qwen endpoint answered a bare 400 "invalid request". Against the same
endpoint the bundled results got the 400 even with a user turn present, and
the same results as one tool message per tool_call_id passed.

Run:
  pytest tests/unit/test_consolidated_tool_response_is_split.py -q
"""
import unittest
from unittest.mock import MagicMock, patch

_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()


def _call(i, key):
    return {'id': 'call_%d' % i, 'type': 'function',
            'function': {'name': 'get_data_by_key',
                         'arguments': '{"key": "%s"}' % key}}


def _central_turn():
    """The four messages Helper's turn carried at 14:19:02, contents shortened."""
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


class ConsolidatedToolResponseTests(unittest.TestCase):
    def setUp(self):
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    def test_a_bundled_reply_is_split_one_tool_message_per_call(self):
        out = self.handler.apply_transform(_central_turn())
        self.assertEqual([m for m in out if 'tool_responses' in m], [],
                         'a bundled tool reply still reaches the model')
        tools = [m for m in out if m.get('role') == 'tool']
        self.assertEqual([m.get('tool_call_id') for m in tools],
                         ['call_1', 'call_2', 'call_3'])
        self.assertEqual(tools[1]['content'], "[{'lead_id': 'l1'}]")

    def test_each_call_is_answered_right_after_its_assistant(self):
        out = self.handler.apply_transform(_central_turn())
        first = next(i for i, m in enumerate(out)
                     if m.get('role') == 'assistant'
                     and len(m.get('tool_calls') or []) == 2)
        answers = out[first + 1:first + 3]
        self.assertEqual([m.get('role') for m in answers], ['tool', 'tool'])
        self.assertEqual({m.get('tool_call_id') for m in answers},
                         {'call_1', 'call_2'})

    def test_a_single_tool_reply_is_left_as_it_was(self):
        out = self.handler.apply_transform([
            {'role': 'user', 'content': 'look it up'},
            {'role': 'assistant', 'content': '', 'tool_calls': [_call(1, 'k')]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'v'},
        ])
        tools = [m for m in out if m.get('role') == 'tool']
        self.assertEqual([(m.get('tool_call_id'), m.get('content')) for m in tools],
                         [('call_1', 'v')])

    def test_a_limited_bundle_stays_limited_after_the_split(self):
        """#104, central 2026-09-14 06:06, Guardian Convergence action 9.

        Two search_long_term_memory results came back bundled, 3,386,616
        chars together. token_limiter cut the bundle's 'content' to its
        allowance, but the split rebuilds each call's message from
        'tool_responses', which the limiter never touched. So the full size
        reached the hosted endpoint and every call was a bare 400 until the
        loop-break fired. This runs the create seats' own chain on that shape.
        """
        from autogen.agentchat.contrib.capabilities import transforms_util
        from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
        from hartos.helper import token_limiter
        store = str({'hive': {'scheduler': {'jobs': [
            {'name': 'guardian_convergence_monitor', 'interval_seconds': 21600,
             'status_history': ['cycle %d steady' % i for i in range(3000)]}]}}})
        turn = [
            {'role': 'user', 'name': 'ChatInstructor',
             'content': 'Execute Action 9: search_long_term_memory for prior '
                        'threat patterns'},
            {'role': 'assistant', 'name': 'Assistant', 'content': '',
             'tool_calls': [_call(1, 'q1'), _call(2, 'q2')]},
            {'role': 'tool', 'name': 'Assistant',
             'content': store + '\n\n' + store,
             'tool_responses': [
                 {'tool_call_id': 'call_1', 'role': 'tool', 'content': store},
                 {'tool_call_id': 'call_2', 'role': 'tool', 'content': store}]},
        ]
        limiter = token_limiter(
            max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
            max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
            min_tokens=0)
        out = self.handler.apply_transform(limiter.apply_transform(turn))
        tools = [m for m in out if m.get('role') == 'tool']
        self.assertEqual([m.get('tool_call_id') for m in tools],
                         ['call_1', 'call_2'])
        for m in tools:
            self.assertLessEqual(
                transforms_util.count_text_tokens(m['content']),
                AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                'a tool result reached the model past the per-message limit')
        self.assertEqual(turn[2]['tool_responses'][0]['content'], store,
                         "the limiter edited the group chat's own message")


if __name__ == '__main__':
    unittest.main()
