"""Wire-trim must be able to truncate the ANCHOR when it is the oversized part.

aa403502 made the newest user message drop-protected (the Qwen3.5 template
500s "No user query found in messages.").  But step 4's char-truncation only
ever touches ``messages[-1]`` — and in the autogen.reuse conversations the
anchor sits mid-list behind assistant/tool replies.  When the anchor ALONE
exceeds the budget, the trim is structurally unable to succeed:

    2026-08-30 19:35-19:46, installed build, source autogen.reuse:
    "[TRIM] trim could not reach budget — ... messages 5597 tok ...
    against n_ctx (budget was 3840)" x95 — every background reuse turn
    was sent doomed and rejected by llama-server.

The fix extends the SAME truncation step to the anchor (one shared
content-truncation helper, two call sites — no second implementation):
left-truncate the anchor's content with WIRE_TRIM_MARKER, exactly the
policy the last message already gets.

    python -m pytest tests/unit/test_wire_trim_truncates_oversized_anchor.py --noconftest -q
"""
import unittest
from unittest.mock import patch

import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_MARKER


def _msgs_with_giant_anchor():
    """The measured shape: system, giant user task (anchor), then replies."""
    return [
        {'role': 'system', 'content': 'You are the reuse action expert.'},
        {'role': 'user', 'content': 'TASK HEAD. ' + ('data ' * 4000) + ' TASK TAIL.'},
        {'role': 'assistant', 'content': 'Working on it.'},
        {'role': 'tool', 'content': 'partial result', 'tool_call_id': 'c1'},
        {'role': 'assistant', 'content': 'Retrying the action now.'},
    ]


class WireTrimTruncatesOversizedAnchor(unittest.TestCase):

    def _trim(self, messages, max_tokens=64):
        body = {'messages': messages, 'max_tokens': max_tokens}
        with patch.object(lol, '_get_budget_per_slot', lambda: 900):
            return lol._trim_to_budget(body)

    def test_giant_anchor_is_truncated_to_fit_the_budget(self):
        trimmed, n_dropped, n_chars, est_before, est_after, budget = \
            self._trim(_msgs_with_giant_anchor())
        self.assertLessEqual(
            est_after, budget,
            f'trim left the request over budget ({est_after} > {budget}) - '
            'the oversized anchor was never truncated, so the request goes '
            'out doomed (measured 95x live on 2026-08-30)')
        roles = [m.get('role') for m in trimmed['messages']]
        self.assertIn('user', roles, 'anchor must survive as a user message')
        a_text = next(m['content'] for m in trimmed['messages']
                      if m.get('role') == 'user')
        self.assertTrue(a_text.startswith(WIRE_TRIM_MARKER),
                        'truncated anchor must carry the wire-trim marker')
        self.assertIn('TASK TAIL.', a_text,
                      'left-truncation must preserve the tail of the task')
        self.assertGreater(n_chars, 0)

    def test_small_anchor_is_left_untouched(self):
        """When drops alone reach the budget, the anchor keeps its content."""
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'short task'},
        ] + [{'role': 'assistant', 'content': 'filler ' * 200}
             for _ in range(6)] + [
            {'role': 'user', 'content': 'newest question'},
        ]
        trimmed, n_dropped, n_chars, est_before, est_after, budget = \
            self._trim(messages)
        self.assertLessEqual(est_after, budget)
        user_texts = [m['content'] for m in trimmed['messages']
                      if m.get('role') == 'user']
        self.assertIn('newest question', user_texts,
                      'the newest user message is the anchor and must be intact')

    def test_anchor_as_last_message_is_not_double_truncated(self):
        """When the anchor IS messages[-1], the existing last-message step
        already handles it - the anchor pass must not cut it twice."""
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'HEAD. ' + ('data ' * 4000) + ' TAIL.'},
        ]
        trimmed, n_dropped, n_chars, est_before, est_after, budget = \
            self._trim(messages)
        self.assertLessEqual(est_after, budget)
        a_text = trimmed['messages'][-1]['content']
        self.assertEqual(a_text.count(WIRE_TRIM_MARKER), 1,
                         'marker must appear exactly once')
        self.assertIn('TAIL.', a_text)


if __name__ == '__main__':
    unittest.main()
