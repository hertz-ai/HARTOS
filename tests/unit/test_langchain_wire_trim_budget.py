"""The langchain requests path must fit the n_ctx budget before it POSTs.

Root cause (live 2026-09-02, weather-in-London turn, installed build
Nunba pid 30956): the langchain ReAct tool loop's follow-up call built a
14,899-token prompt (the re-rendered full tool schema + a large
google_search observation) and CustomGPT._call POSTed it through
``_pooled_post_with_refusal_check`` -> ``core.http_pool.pooled_post`` (the
requests pool).  The canonical wire-trim
(``core.llm_outbound_logger._apply_trim_to_request``) only fires on the
httpx layer, so the requests path bypassed it — its own architecture note
names "raw requests.post" as the bypass — and llama-server 500'd with
"request (14899 tokens) exceeds the available context size (12288 tokens)".
The turn then returned the generic "I ran into a problem handling that."

Fix: ``_pooled_post_with_refusal_check`` routes the body through the SAME
``_trim_to_budget`` before posting (idempotent; no second implementation).
This test drives an over-budget body through the wrapper with pooled_post
mocked and asserts the body that reaches the wire fits the per-slot budget.

RED before the fix (the untrimmed ~20k-token body is sent as-is, ~20k >
12288); GREEN after.

    python -m pytest tests/unit/test_langchain_wire_trim_budget.py --noconftest -q
"""
import os
import unittest
from unittest.mock import MagicMock, patch


def _resp(content):
    r = MagicMock()
    r.json.return_value = {'choices': [{'message': {'content': content}}]}
    return r


class TestLangchainWireTrimBudget(unittest.TestCase):
    def test_oversized_body_is_trimmed_to_fit_before_post(self):
        import hart_intelligence_entry as hie
        from core.llm_outbound_logger import _get_budget_per_slot
        from core.token_utils import count_tokens_for_messages

        # A single user message far over budget — the shape the langchain
        # ReAct follow-up produced live (tool schema + big observation).
        huge = 'weather data ' * 8000  # ~104k chars ≈ ~20k tokens
        body = {
            'model': 'test-model',
            'messages': [{'role': 'user', 'content': huge}],
            'max_tokens': 200,
        }
        # A non-refusal "Final Answer" reply so the refusal-override does not
        # retry — we want exactly one POST to inspect.
        pooled = MagicMock(return_value=_resp('Here is the forecast.'))

        with patch.object(hie, 'pooled_post', pooled), \
                patch.dict(os.environ, {
                    'HEVOLVE_LANGCHAIN_REFUSAL_OVERRIDE': '1',
                    'HEVOLVE_LLAMA_SLOTS': '1',
                }, clear=False):
            per_slot = _get_budget_per_slot()
            hie._pooled_post_with_refusal_check(
                'http://127.0.0.1:8080/v1/chat/completions',
                json=body, app_logger=MagicMock())

            self.assertEqual(pooled.call_count, 1, 'expected exactly one POST')
            sent = pooled.call_args.kwargs.get('json') or pooled.call_args[1].get('json')
            self.assertIsNotNone(sent, 'POST carried no json body')
            sent_tokens = count_tokens_for_messages(
                sent.get('messages') or [], sent.get('model'))

            # Precondition: the ORIGINAL body really was over budget, else the
            # assertion below would pass vacuously (nothing to trim).
            orig_tokens = count_tokens_for_messages(body['messages'], body['model'])
            self.assertGreater(
                orig_tokens, per_slot,
                f'test body ({orig_tokens} tok) is not over the per-slot '
                f'budget ({per_slot}); it cannot exercise the trim')

            # The fix: the body that reached the wire fits the per-slot n_ctx.
            self.assertLessEqual(
                sent_tokens, per_slot,
                f'body reached llama-server at {sent_tokens} tok > per-slot '
                f'budget {per_slot} — the requests path bypassed the wire-trim '
                f'(pre-fix behaviour); it would 500 with "exceeds the '
                f'available context size"')


if __name__ == '__main__':
    unittest.main()
