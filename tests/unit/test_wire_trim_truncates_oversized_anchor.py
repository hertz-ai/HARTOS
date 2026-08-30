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
        """max_tokens=None omits the field entirely (the autogen.reuse shape)."""
        body = {'messages': messages}
        if max_tokens is not None:
            body['max_tokens'] = max_tokens
        with patch.object(lol, '_get_budget_per_slot', lambda: 900):
            return lol._trim_to_budget(body)

    def test_missing_max_tokens_is_pinned_to_the_budgeted_default(self):
        """The budget math reserves max_tokens (default 2048) out of every
        slot — but autogen.reuse bodies carry NO max_tokens field, so
        llama-server generated unbounded.  Measured 2026-08-30 (llama rel
        11.36-11.40, installed build): task 7256 reached n_decoded=3,975,
        hit its 6,144 slot ceiling ('Context size has been exceeded') AND
        exhausted the shared batch memory ('failed to find a memory slot
        for batch of size 467'), collateral-failing a concurrent request
        whose 3,039 real tokens fit comfortably (#734).  The wire must
        enforce the same default it budgets with — on BOTH the untrimmed
        and trimmed paths."""
        trimmed, *_ = self._trim([{'role': 'user', 'content': 'hi'}],
                                 max_tokens=None)
        self.assertEqual(trimmed.get('max_tokens'), 2048,
                         'under-budget path must pin the budgeted default')
        trimmed2, *_ = self._trim([
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'x ' * 8000},
        ], max_tokens=None)
        self.assertEqual(trimmed2.get('max_tokens'), 2048,
                         'trimmed path must pin the budgeted default')

    def test_explicit_max_tokens_is_preserved(self):
        trimmed, *_ = self._trim([{'role': 'user', 'content': 'hi'}],
                                 max_tokens=64)
        self.assertEqual(trimmed.get('max_tokens'), 64)

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

    def test_giant_system_message_is_truncated_as_last_resort(self):
        """The dominant live failure (86 of 100 STILL-over, 2026-08-30
        20:06-20:20): autogen.reuse builds a system message carrying the
        persona boilerplate PLUS the whole serialized recipe (~28k chars,
        sample [system 28154c, assistant 247c]) — over the per-slot budget
        on its own.  Dropping and truncating other messages can never fix
        that, so every such turn was sent doomed and rejected.  As a LAST
        resort (everything else already trimmed), the system content gets
        the same left-truncation: the boilerplate head is cut, the
        actionable recipe tail survives."""
        messages = [
            {'role': 'system',
             'content': 'CULTURAL BOILERPLATE. ' + ('wisdom ' * 4000)
                        + ' <recipeEnd> ACTIONABLE TAIL.'},
            {'role': 'assistant', 'content': 'Short reply.'},
        ]
        trimmed, n_dropped, n_chars, est_before, est_after, budget = \
            self._trim(messages)
        self.assertLessEqual(
            est_after, budget,
            f'trim left the request over budget ({est_after} > {budget}) - '
            'the oversized system message was never truncated, so the '
            'request goes out doomed (measured 86x live)')
        sys_text = trimmed['messages'][0]['content']
        self.assertTrue(sys_text.startswith(WIRE_TRIM_MARKER))
        self.assertIn('ACTIONABLE TAIL.', sys_text,
                      'left-truncation must keep the system tail')

    def test_small_system_is_never_touched(self):
        """System stays intact whenever anything else can absorb the cut."""
        messages = [
            {'role': 'system', 'content': 'small system prompt'},
            {'role': 'user', 'content': 'HEAD. ' + ('data ' * 4000) + ' TAIL.'},
        ]
        trimmed, *_ = self._trim(messages)
        self.assertEqual(trimmed['messages'][0]['content'],
                         'small system prompt')

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
