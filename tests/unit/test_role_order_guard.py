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


class RoleOrderGuardToolResultTests(unittest.TestCase):
    """A tool RESULT must never be coalesced away.

    The guard already refuses to coalesce a message that CARRIES tool_calls
    (test_tool_call_carrying_assistants_are_NOT_coalesced, and rule 3 in this
    module's docstring: "would silently drop the call").  That protects the
    question.  It never protected the ANSWER: a role='tool' message has no
    'tool_calls' key, so has_calls is False and two consecutive results fall
    straight into the same-role merge.

    Measured live 2026-09-07 on the installed build (gui_app.log.1, 104
    occurrences across two rotations), e.g. 03:47:27 —

        [ROLE-ORDER-GUARD] 34 msgs out of 54 in; ... coalesced 20
        consecutive same-role pair(s) at 0+1(user), 10+11(tool), 11+12(tool),
        12+13(tool), 13+14(tool), 14+15(tool), 15+16(tool), ...

    Seven tool results at indices 10-16 collapsed into one message.  The merged
    message keeps only the FIRST result's tool_call_id, so the other six
    assistant tool_calls are left unanswered; ToolMessageHandler then stamps
    HISTORICAL_TOOL_PLACEHOLDER into each empty answer slot (helper.py:1889),
    the real output is gone from the model's context, and the fabrication guard
    correctly refuses to count a placeholder as execution.

    Coalescing tool messages was never needed for the guard's stated purpose.
    The OpenAI alternation rule it exists to satisfy is about user/assistant;
    consecutive role='tool' messages are REQUIRED — exactly one per tool_call
    in a parallel-call assistant message.
    """

    def setUp(self):
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    def test_consecutive_tool_results_are_NOT_coalesced(self):
        """Two calls, two answers — both must survive with their own id."""
        messages = [
            {'role': 'user', 'content': 'do A and B'},
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [
                    {'id': 'a1', 'type': 'function',
                     'function': {'name': 'doA', 'arguments': '{}'}},
                    {'id': 'b1', 'type': 'function',
                     'function': {'name': 'doB', 'arguments': '{}'}},
                ],
            },
            {'role': 'tool', 'tool_call_id': 'a1', 'content': 'result A'},
            {'role': 'tool', 'tool_call_id': 'b1', 'content': 'result B'},
        ]
        out = self.handler.validate_messages(messages)

        answered = {m.get('tool_call_id') for m in out if m['role'] == 'tool'}
        self.assertEqual(
            answered, {'a1', 'b1'},
            "a tool result was coalesced away — the assistant's tool_call is "
            "left unanswered and gets a HISTORICAL_TOOL_PLACEHOLDER stamped "
            "over the real output",
        )
        self.assertEqual(len(out), 4)

    def test_seven_tool_results_all_survive(self):
        """The measured live shape: indices 10-16, seven results, one call
        each.  Pre-fix this returns a single merged tool message."""
        messages = [{'role': 'user', 'content': 'go'}]
        ids = [f'call_{i}' for i in range(7)]
        messages.append({
            'role': 'assistant', 'content': '',
            'tool_calls': [
                {'id': i, 'type': 'function',
                 'function': {'name': 'f', 'arguments': '{}'}} for i in ids
            ],
        })
        for i in ids:
            messages.append(
                {'role': 'tool', 'tool_call_id': i, 'content': f'output {i}'})

        out = self.handler.validate_messages(messages)

        tool_msgs = [m for m in out if m['role'] == 'tool']
        self.assertEqual(
            len(tool_msgs), 7,
            f"7 tool results in, {len(tool_msgs)} out — the rest were merged "
            f"into a sibling and their tool_call_ids lost",
        )
        self.assertEqual({m['tool_call_id'] for m in tool_msgs}, set(ids))
        for i in ids:
            self.assertTrue(
                any(f'output {i}' in m['content'] for m in tool_msgs),
                f"real output for {i} did not survive the guard",
            )

    def test_tool_results_are_not_merged_into_a_neighbouring_role(self):
        """Narrowing the merge must not push a result into the user or
        assistant message beside it — the content has to stay addressable
        by tool_call_id, not just present somewhere in the list."""
        messages = [
            {'role': 'user', 'content': 'q'},
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'x1', 'type': 'function',
                                'function': {'name': 'f', 'arguments': '{}'}}],
            },
            {'role': 'tool', 'tool_call_id': 'x1', 'content': 'the answer'},
            {'role': 'user', 'content': 'next'},
        ]
        out = self.handler.validate_messages(messages)

        tool_msgs = [m for m in out if m['role'] == 'tool']
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]['tool_call_id'], 'x1')
        self.assertEqual(tool_msgs[0]['content'], 'the answer')

    def test_a_tool_result_reading_TERMINATE_is_not_dropped(self):
        """Sibling site, same invariant: the stale-TERMINATE drop must skip
        tool messages.  A result that happens to read TERMINATE is a RESULT,
        and dropping it orphans its call exactly like coalescing does.

        Not observed in production — pinned here so the invariant holds at
        both sites rather than only the one that was measured.
        """
        messages = [
            {'role': 'user', 'content': 'run it'},
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 't1', 'type': 'function',
                                'function': {'name': 'f', 'arguments': '{}'}}],
            },
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'TERMINATE'},
            {'role': 'assistant', 'content': 'done'},
        ]
        out = self.handler.validate_messages(messages)

        self.assertEqual(
            [m.get('tool_call_id') for m in out if m['role'] == 'tool'], ['t1'],
            "the tool result was dropped as a stale control token",
        )

    def test_a_bare_TERMINATE_from_an_assistant_is_still_dropped(self):
        """The narrowing must not weaken the drop it was carved out of —
        a consumed TERMINATE that is NOT a tool result still goes."""
        messages = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': 'TERMINATE'},
            {'role': 'user', 'content': 'again'},
        ]
        out = self.handler.validate_messages(messages)
        self.assertNotIn(
            'TERMINATE', ' '.join(str(m.get('content')) for m in out),
            "stale TERMINATE drop regressed",
        )

    def test_user_and_assistant_coalescing_still_happens(self):
        """The narrowing must be exactly that — the guard's actual purpose
        (the user/assistant alternation rule that caused the 2026-05-08 400)
        must be untouched, in the same list that carries a tool result."""
        messages = [
            {'role': 'user', 'content': 'first'},
            {'role': 'user', 'content': 'second'},
            {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'z1', 'type': 'function',
                                'function': {'name': 'f', 'arguments': '{}'}}],
            },
            {'role': 'tool', 'tool_call_id': 'z1', 'content': 'out'},
            {'role': 'assistant', 'content': 'part 1'},
            {'role': 'assistant', 'content': 'part 2'},
        ]
        out = self.handler.validate_messages(messages)

        users = [m for m in out if m['role'] == 'user']
        self.assertEqual(len(users), 1, "user/user coalescing was lost")
        self.assertIn('first', users[0]['content'])
        self.assertIn('second', users[0]['content'])

        tail = [m for m in out
                if m['role'] == 'assistant' and not m.get('tool_calls')]
        self.assertEqual(len(tail), 1, "assistant/assistant coalescing was lost")
        self.assertIn('part 1', tail[0]['content'])
        self.assertIn('part 2', tail[0]['content'])


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
