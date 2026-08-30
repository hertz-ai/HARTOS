"""#124 — regression test for ToolMessageHandler's ROLE-ORDER-GUARD.

Pins the contract introduced at helper.py:650-725 (added 2026-05-08
after live evidence of OpenAI-API 400 "Cannot have 2 or more assistant
messages at the end of the list"):

  1. Empty-content assistant messages (no tool_calls / function_call)
     are dropped — they're autogen speaker-selection placeholders.
  2. Consecutive same-role messages get coalesced into one with
     content joined by two newlines.
  3. Tool-call / function-call carrying messages are NEVER coalesced
     (would silently drop the call) — they pass through as-is.
  4. The guard is best-effort: any internal exception falls through
     with the original messages intact rather than blocking the
     pipeline.
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# Stub current_app.logger before helper imports — helper.py uses
# `from flask import current_app` at the top.  We don't need Flask
# in the test, just a no-raise logger.
_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()


class RoleOrderGuardTests(unittest.TestCase):
    def setUp(self):
        # Patch current_app for the duration of each test.
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    def test_empty_assistant_placeholder_is_dropped(self):
        """An assistant message with empty content AND no tool_calls is
        an autogen speaker-selection artifact — drop it before it
        causes a downstream 400 alternation error."""
        messages = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': ''},     # placeholder — DROP
            {'role': 'assistant', 'content': 'Hi!'},
        ]
        out = self.handler.validate_messages(messages)
        roles = [(m['role'], m.get('content')) for m in out]
        self.assertEqual(roles, [('user', 'hello'), ('assistant', 'Hi!')])

    def test_assistant_with_tool_calls_is_kept_even_when_empty_content(self):
        """An assistant emitting tool_calls legitimately has empty
        text content — must NOT be dropped."""
        messages = [
            {'role': 'user', 'content': 'add 2+2'},
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{'id': 'call_1', 'type': 'function',
                                'function': {'name': 'add', 'arguments': '{"a":2,"b":2}'}}],
            },
            {'role': 'tool', 'content': '4', 'tool_call_id': 'call_1'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertEqual(len(out), 3)
        self.assertIn('tool_calls', out[1])

    def test_consecutive_assistants_are_coalesced(self):
        """Two assistant messages in a row get merged into one with
        content joined by \\n\\n.  Prevents the 'consecutive same role'
        OpenAI 400."""
        messages = [
            {'role': 'user', 'content': 'multi-step?'},
            {'role': 'assistant', 'content': 'Step 1: analyze.'},
            {'role': 'assistant', 'content': 'Step 2: execute.'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]['role'], 'assistant')
        self.assertIn('Step 1: analyze.', out[1]['content'])
        self.assertIn('Step 2: execute.', out[1]['content'])
        self.assertIn('\n\n', out[1]['content'])

    def test_consecutive_users_are_coalesced(self):
        """Same coalesce rule applies to consecutive user messages
        (sometimes triggered by the autogen UserProxyAgent emitting
        a back-to-back follow-up)."""
        messages = [
            {'role': 'user', 'content': 'first part'},
            {'role': 'user', 'content': 'second part'},
            {'role': 'assistant', 'content': 'ack'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]['role'], 'user')
        self.assertIn('first part', out[0]['content'])
        self.assertIn('second part', out[0]['content'])

    def test_tool_call_carrying_assistants_are_NOT_coalesced(self):
        """Two assistant messages BOTH carrying tool_calls must NOT be
        merged — coalescing would silently drop one of the calls."""
        messages = [
            {'role': 'user', 'content': 'do A and B'},
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'a1', 'type': 'function',
                                'function': {'name': 'doA', 'arguments': '{}'}}],
            },
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'b1', 'type': 'function',
                                'function': {'name': 'doB', 'arguments': '{}'}}],
            },
        ]
        out = self.handler.validate_messages(messages)
        # Both kept — neither dropped, neither merged.
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]['tool_calls'][0]['id'], 'a1')
        self.assertEqual(out[2]['tool_calls'][0]['id'], 'b1')

    def test_alternating_messages_pass_through_unchanged(self):
        """When messages already alternate user/assistant correctly,
        the guard is a no-op."""
        messages = [
            {'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2'},
            {'role': 'assistant', 'content': 'a2'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertEqual(out, messages)

    def test_three_consecutive_assistants_collapse_to_one(self):
        """The 2026-05-08 live incident: Message[7,8,10] all assistant.
        Empty-placeholder at [8] dropped, then [7] and [10] coalesce
        into a single merged assistant."""
        messages = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': 'part 1'},
            {'role': 'assistant', 'content': ''},          # placeholder — drop
            {'role': 'assistant', 'content': 'part 3'},
        ]
        out = self.handler.validate_messages(messages)
        # user + one merged assistant
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]['role'], 'assistant')
        self.assertIn('part 1', out[1]['content'])
        self.assertIn('part 3', out[1]['content'])


class RoleOrderGuardLogCardinalityTests(unittest.TestCase):
    """#623 — the guard's LOGGING must cost O(1) lines per invocation.

    The guard's diagnostics were correct and deliberate ("surfaces dropped /
    merged events at INFO so future diagnoses are visible") but emitted one
    INFO per affected message.  Measured on a live desktop 2026-08-05, that
    made it the single largest consumer of disk: 15,855 lines / 3.45 MB inside
    one 400k-line sample, gui_app.log growing 3.4 MB/min, log dir 492 MB
    against 23 GB free.  create_recipe.py:133 records the extreme — 20,920 of
    these lines in one session during the livelock.

    These tests pin the CARDINALITY, which is the property that regressed, and
    they discriminate: against the pre-fix code the first one sees 12 info
    calls, not 1, and fails.  Behaviour is pinned by RoleOrderGuardTests above
    and must not change — that separation is deliberate, so a future edit that
    "fixes" logging by also dropping messages fails the other class.
    """

    def setUp(self):
        self._logger = MagicMock()
        fake_app = MagicMock()
        fake_app.logger = self._logger
        self._patcher = patch('hartos.helper.current_app', fake_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    def test_many_dropped_placeholders_emit_exactly_one_log_line(self):
        """12 drops must produce 1 INFO, not 12.  FAILS PRE-FIX (12 calls)."""
        messages = [{'role': 'user', 'content': 'hello'}]
        for _ in range(12):
            messages.append({'role': 'assistant', 'content': ''})
        messages.append({'role': 'assistant', 'content': 'Hi!'})

        out = self.handler.validate_messages(messages)

        # Behaviour unchanged: every placeholder still dropped.
        self.assertEqual(
            [(m['role'], m.get('content')) for m in out],
            [('user', 'hello'), ('assistant', 'Hi!')],
        )
        self.assertEqual(
            self._logger.info.call_count, 1,
            f"expected exactly 1 summary line, got "
            f"{self._logger.info.call_count} — per-message logging is back",
        )

    def test_the_one_line_still_carries_the_counts(self):
        """Bounding volume must not cost the diagnostic.  A summary that
        omits the counts would pass the cardinality test above while being
        useless, so assert the payload too."""
        messages = [
            {'role': 'user', 'content': 'a'},
            {'role': 'assistant', 'content': ''},
            {'role': 'assistant', 'content': ''},
            {'role': 'assistant', 'content': 'x'},
            {'role': 'assistant', 'content': 'y'},
        ]
        self.handler.validate_messages(messages)

        self.assertEqual(self._logger.info.call_count, 1)
        line = self._logger.info.call_args[0][0]
        self.assertIn('[ROLE-ORDER-GUARD]', line)
        self.assertIn('dropped 2', line)
        self.assertIn('coalesced 1', line)

    def test_silent_when_the_guard_had_nothing_to_do(self):
        """Clean alternating input is the common case; it must log nothing.
        This is most of the saving — the guard runs on every turn."""
        messages = [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertEqual(len(out), 2)
        self.assertEqual(self._logger.info.call_count, 0)

    def test_index_list_is_capped_but_count_stays_exact(self):
        """A pathological turn must not reintroduce unbounded growth through
        the index list.  Count exact, indices truncated with '+N more'."""
        messages = [{'role': 'user', 'content': 'hello'}]
        for _ in range(50):
            messages.append({'role': 'assistant', 'content': ''})
        messages.append({'role': 'assistant', 'content': 'done'})

        self.handler.validate_messages(messages)

        self.assertEqual(self._logger.info.call_count, 1)
        line = self._logger.info.call_args[0][0]
        self.assertIn('dropped 50', line)      # count exact
        self.assertIn('more)', line)           # indices truncated
        self.assertLess(len(line), 800, "summary line is growing with input")


if __name__ == '__main__':
    unittest.main()
