"""The wire-layer trim budget must charge for the tool schema, not just messages.

THE BUG
───────
`_trim_to_budget` computed `budget = n_ctx/slots - max_tokens - safety` and then
compared it against `count_tokens_for_messages(messages)` alone. llama-server bills
prompt tokens for the SERIALISED TOOL SCHEMA exactly like message content, so the
single largest consumer was invisible to the layer whose stated job is "the only
place we can guarantee zero context-overflow 500s across all frameworks".

Measured 2026-08-07 over 1,407 real requests in ~/Documents/Nunba/logs/
llm_outbound.jsonl: 29 carried a tools block, and that block was 67 tools ≈ 10,713
tokens — 2.6x an entire 4096 window, 87% of a 12288 one. All 29 overflowed.
The guard reported them as fitting and passed them through untouched.

WHY NOTHING UPSTREAM CAUGHT IT (CLAUDE.md, logs table): autogen attaches
system_message + tools AFTER `transform_messages` runs, so the frozen_debug
"=== FULL INPUT MESSAGES DEBUG ===" dump is a messages-only view. The wire layer is
the only place the tools block is observable before it reaches the socket.

These are behavioural tests — they call the real `_trim_to_budget` and assert on
its returned budget and on the trimming it actually performs. No source-shape
assertions; the sibling ctx-size drift guard covers what cannot be executed.
"""
import importlib
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def _mod():
    """Resolve core.llm_outbound_logger FRESH on every call. Never module-scope.

    tests/unit/test_llm_outbound_logger.py::_reset_module does
    `del sys.modules['core.llm_outbound_logger']` so it can re-import the module
    against a patched log dir. A module-scope `from ... import _trim_to_budget`
    here would bind the function object from the module that existed at COLLECTION
    time, whose __globals__ belong to that now-discarded module. Every
    `patch('core.llm_outbound_logger.X')` would then patch the RE-IMPORTED module
    while the held function kept reading the old one — the patch silently does
    nothing.

    That is exactly what happened: this file passed in isolation and failed when run
    after test_llm_outbound_logger.py, because _get_budget_per_slot was never
    actually patched to 4096 and the oversized-schema branch was never reached.
    Same failure class as the patch.dict(sys.modules, ...) teardown bug fixed in
    test_personas_empty_does_not_crash_chat.py.

    Resolving through importlib per call means we always patch and call the SAME
    module object, whatever ran before us.
    """
    return importlib.import_module('core.llm_outbound_logger')


def _trim_to_budget(body):
    return _mod()._trim_to_budget(body)


def _tools(n, desc_len=400):
    """A tools block shaped like the real one: n functions with fat descriptions."""
    return [{
        'type': 'function',
        'function': {
            'name': 'tool_%d' % i,
            'description': 'x' * desc_len,
            'parameters': {
                'type': 'object',
                'properties': {'arg': {'type': 'string', 'description': 'y' * 80}},
                'required': ['arg'],
            },
        },
    } for i in range(n)]


def _body(messages, tools=None, max_tokens=512):
    b = {'model': 'llama', 'messages': messages, 'max_tokens': max_tokens}
    if tools:
        b['tools'] = tools
    return b


class TheBudgetChargesForTheToolSchema(unittest.TestCase):

    #: n_ctx large enough that a small message set fits comfortably, so any
    #: difference in the returned budget is attributable to the tools block alone.
    NCTX = 12288

    def _budget_for(self, body):
        with patch('core.llm_outbound_logger._get_budget_per_slot',
                   return_value=self.NCTX):
            return _trim_to_budget(body)[5]

    def test_a_tools_block_REDUCES_the_available_budget(self):
        """The exact defect: tools were free, so the budget was overstated."""
        msgs = [{'role': 'user', 'content': 'hello'}]
        without = self._budget_for(_body(msgs))
        with_tools = self._budget_for(_body(msgs, tools=_tools(67)))
        self.assertLess(
            with_tools, without,
            "the tool schema did not reduce the budget at all — the trim layer is "
            "still blind to it, which is how 67 tools (~10,713 tokens) passed "
            "through a 4096 window reported as fitting")

    def test_the_reduction_is_PROPORTIONAL_to_the_schema_size(self):
        """A bigger schema must cost more; a fixed fudge factor is not a fix."""
        msgs = [{'role': 'user', 'content': 'hello'}]
        small = self._budget_for(_body(msgs, tools=_tools(5)))
        large = self._budget_for(_body(msgs, tools=_tools(50)))
        self.assertLess(
            large, small,
            "50 tools cost the same budget as 5 — the schema is not actually being "
            "measured")

    def test_a_request_that_only_fits_WITHOUT_tools_is_now_trimmed(self):
        """The observable consequence: it used to be passed through untouched.

        Deliberately realistic on both axes. The tool schema is large but FITTABLE
        (a schema bigger than n_ctx takes the separate unfittable-degrade path,
        covered by ItSaysSoWhenTheSchemaAloneCannotFit). The message body is ordinary
        prose, because the truncation step estimates at a fixed 3.5 chars/token —
        synthetic filler like 'w ' runs ~2 chars/token, dense enough that the
        estimate over-keeps and the assertion would be testing that approximation
        rather than the tools accounting this file is about.

        MEASURED — prose is 28,340 chars = 4,690 tokens; an 80-tool schema is
        ~10,161 tokens. Against n_ctx 12288 that leaves a budget of 1,359, so the
        message must be cut. Without the schema the budget is 11,520 and the same
        message sails through untouched. The delta IS the bug.
        """
        prose = ('The department has not responded to the previous notice and the '
                 'matter remains unresolved for the applicant. ') * 260
        msgs = [{'role': 'system', 'content': 'sys'},
                {'role': 'user', 'content': prose}]
        with patch('core.llm_outbound_logger._get_budget_per_slot',
                   return_value=self.NCTX):
            plain = _trim_to_budget(_body(msgs))
            _, n_dropped, n_trunc, est_before, est_after, budget = \
                _trim_to_budget(_body(msgs, tools=_tools(80)))
        self.assertEqual(
            (0, 0), (plain[1], plain[2]),
            "precondition broken: this message set was supposed to FIT without "
            "tools, so any trimming below is attributable to the schema")
        self.assertTrue(
            n_dropped or n_trunc,
            "nothing was dropped or truncated even though the tool schema pushed "
            "the request over n_ctx — this is the exact pass-through the fix removes")
        self.assertLessEqual(
            est_after, budget,
            "the request was left over the (tools-aware) budget — it will be "
            "rejected by llama-server as over-length")

    def test_no_tools_key_behaves_EXACTLY_as_before(self):
        """Zero regression for the 97.9% of requests that carry no tools."""
        msgs = [{'role': 'user', 'content': 'hello'}]
        with patch('core.llm_outbound_logger._get_budget_per_slot',
                   return_value=self.NCTX):
            plain = _trim_to_budget(_body(msgs))
            empty = _trim_to_budget(_body(msgs, tools=[]))
        self.assertEqual(plain[5], empty[5],
                         "an empty tools list changed the budget; it costs nothing "
                         "and must be indistinguishable from no tools at all")

    def test_it_does_not_CRASH_on_an_unserialisable_tools_block(self):
        """Degrade, never raise — this runs on the outbound path of every call."""
        msgs = [{'role': 'user', 'content': 'hello'}]
        body = _body(msgs, tools=[{'fn': object()}])   # not JSON-serialisable
        with patch('core.llm_outbound_logger._get_budget_per_slot',
                   return_value=self.NCTX):
            try:
                budget = _trim_to_budget(body)[5]
            except Exception as exc:                    # noqa: BLE001
                self.fail("an unserialisable tools block raised %r — this would "
                          "break every outbound LLM call" % exc)
        self.assertLess(
            budget, self.NCTX,
            "an unserialisable tools block was charged nothing, which understates "
            "the cost in exactly the direction that causes overflow")


class ItSaysSoWhenTheSchemaAloneCannotFit(unittest.TestCase):
    """Silent degrade is what hid this for so long."""

    def test_an_oversized_tool_schema_is_logged_at_ERROR(self):
        msgs = [{'role': 'user', 'content': 'hello'}]
        # 4096 n_ctx against the real ~10.7k-token schema shape: unfittable.
        with patch('core.llm_outbound_logger._get_budget_per_slot',
                   return_value=4096), \
                patch('core.llm_outbound_logger.logger') as log:
            _trim_to_budget(_body(msgs, tools=_tools(67)))
        self.assertTrue(
            log.error.called,
            "the tool schema alone exceeded n_ctx and nothing was logged at ERROR. "
            "Message trimming cannot recover a schema that does not fit, so a quiet "
            "degrade means the request goes out over-length and is rejected — the "
            "operator needs to be told to prune the tool list.")
        msg = ' '.join(str(a) for a in log.error.call_args[0])
        self.assertIn('TOOL SCHEMA', msg,
                      "the error does not name the tool schema as the cause")


if __name__ == '__main__':
    unittest.main()
