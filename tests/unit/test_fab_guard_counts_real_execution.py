"""Guard: the fabrication gate counts REAL tool execution only.

Live root cause 2026-09-06 (agent 89555447799, installed build, reuse turn
14:50-15:11).  Nine consecutive actions (16..24) advanced on a model-asserted
"completed" verdict and FAB-GUARD passed every one with unrun=[] -- while the
tool bodies did not run: `INSIDE execute_windows_or_android_command` fired
ONCE in 21 minutes and `INSIDE google_search` ZERO times.

Why the guard could not fail.  `executed` was fed by two things that are the
SIGNATURE OF NON-EXECUTION:

  1. role=='tool' messages -- including the synthetic placeholders helper.py
     mints for a tool_call that never returned.  Measured on the wire
     (llm_outbound.jsonl, 625 bodies): of every named tool-role message,
     placeholder=57/23/22/10/... and real=0.  EVERY ONE was a placeholder.
  2. `tool_calls` entries -- which are the model PROPOSING a call, not the
     tool running.  1,200 proposals for execute_windows_or_android_command
     alone.

Because reuse runs with clear_history=False the set also accumulates for the
whole session and never resets, so by action 16 it held the entire 25-name
roster (including 'Assistant', an AGENT name that reached it via a
placeholder's `name` fallback) and unrun=[] was structurally unreachable.

That is a vacuous guard in the strict sense (feedback_vacuous_guards): it
cannot fail for its own defect.  Its whole purpose is catching "claimed
completed with no tool execution", and it was being fed by the evidence of
no tool execution.

Fix: count a tool as executed only when a role=='tool' message carries a REAL
result -- not the placeholder sentinel -- and never on a bare proposal.  The
sentinel lives in core.constants so the minter (helper) and the reader
(reuse_recipe) cannot drift apart.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.constants import HISTORICAL_TOOL_PLACEHOLDER  # noqa: E402


class _Task:
    def __init__(self, text):
        self._text = text

    def get_action(self, _idx):
        return self._text


class _Agent:
    def __init__(self, tool_names, conv=None):
        self._function_map = {n: (lambda: None) for n in tool_names}
        self._oai_messages = conv or {}
        self.llm_config = None


class _GroupChat:
    def __init__(self, messages):
        self.messages = messages


def _guard():
    from hartos import reuse_recipe
    return reuse_recipe


class FabGuardCountsRealExecution(unittest.TestCase):
    """Drives the real _reuse_fabricated_tools, not a reimplementation."""

    TOOL = 'execute_windows_or_android_command'

    def setUp(self):
        self.rr = _guard()
        self.key = 'u1_p1'
        self.rr.user_tasks[self.key] = _Task(
            f'Run {self.TOOL} to publish the campaign')

    def tearDown(self):
        self.rr.user_tasks.pop(self.key, None)

    def test_placeholder_result_is_not_execution(self):
        """A placeholder exists BECAUSE the call never returned."""
        msgs = [
            {'role': 'assistant',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': self.TOOL}}]},
            {'role': 'tool', 'name': self.TOOL, 'tool_call_id': 'c1',
             'content': HISTORICAL_TOOL_PLACEHOLDER},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL])])
        self.assertEqual(
            unrun, [self.TOOL],
            "a placeholder-only tool must be reported UNRUN — the placeholder "
            "is minted precisely because the call produced no result")

    def test_bare_proposal_is_not_execution(self):
        """tool_calls is the model asking; it is not the tool running."""
        msgs = [
            {'role': 'assistant',
             'tool_calls': [{'id': 'c2', 'type': 'function',
                             'function': {'name': self.TOOL}}]},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL])])
        self.assertEqual(
            unrun, [self.TOOL],
            "a proposed-but-unanswered tool_call must be reported UNRUN")

    def test_real_result_still_counts_as_execution(self):
        """Fail-open must survive: a genuine result clears the tool.

        This is the property the 2026-09-05 Trading widening protected, and it
        must not regress — a real result found in ANY scanned buffer counts.
        """
        msgs = [
            {'role': 'assistant',
             'tool_calls': [{'id': 'c3', 'type': 'function',
                             'function': {'name': self.TOOL}}]},
            {'role': 'tool', 'name': self.TOOL, 'tool_call_id': 'c3',
             'content': 'Successfully ran the command in user\'s computer.'},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL])])
        self.assertEqual(unrun, [],
                         "a REAL tool result must clear the tool as executed")

    def test_real_result_in_a_pairwise_buffer_counts(self):
        """Buffer-widening preserved: results can live outside the group log."""
        conv = {'peer': [
            {'role': 'tool', 'name': self.TOOL, 'tool_call_id': 'c4',
             'content': 'real output here'},
        ]}
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat([]), [_Agent([self.TOOL], conv=conv)])
        self.assertEqual(unrun, [],
                         "a real result in an agent's pairwise _oai_messages "
                         "buffer must still count as executed")

    def test_unrelated_tool_does_not_clear(self):
        """revwarm5407: a DIFFERENT tool running must not clear a named one.

        This is the property test_reuse_fabguard_scans_agent_buffers used to
        pin by text against the tool_calls expression.  Pinned behaviourally
        here instead, so removing that expression cannot silently lose it.
        """
        other = 'search_long_term_memory'
        msgs = [
            {'role': 'tool', 'name': other, 'tool_call_id': 'c9',
             'content': 'a real result, but for a DIFFERENT tool'},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL, other])])
        self.assertEqual(
            unrun, [self.TOOL],
            "an unrelated tool's real result must not mark the action's own "
            "named tool as executed")

    def test_tool_reported_failure_is_not_execution(self):
        """A tool that RAN and reported failure has not done the action's work.

        Live 2026-09-07, agent 60834540771 driven as its real owner
        (c23d388c-...), action 1 "Bring the HART Finance Dashboard window to
        foreground".  execute_windows_or_android_command really did run: the
        VLM computer-use loop clicked the taskbar at (2206,1373), opened a
        Notepad error dialog, spent its remaining iterations dismissing it and
        exited `max_iterations` / status=incomplete.  The tool then returned
        its failure branch (reuse_recipe.py:1929) VERBATIM as the string
        below, and 25s later the action advanced:

            02:11:34  "content": "Not able to perform this action now please try later"
            02:11:59  reuse-w1-completed: terminal 'completed' verdict for action 1
            02:11:59  [FAB-GUARD] ... executed=[...]; unrun=[]

        The guard passed because it asked "did a result come back", never "did
        the tool do the work".  So the one thing it exists to prevent -- an
        action reported complete without its tool's work behind it -- happened
        with the guard green, which is the vacuous-guard shape
        (feedback_vacuous_guards): it could not fail for its own defect.

        Reporting UNRUN routes this into the machinery that already exists for
        it: _advance_reuse_action re-steers (bounded), and only after
        _REUSE_FAB_STEER_MAX real re-steers advances anyway and LOUDLY, so a
        non-tool-backed action is never silently reported as verified.
        """
        msgs = [
            {'role': 'assistant',
             'tool_calls': [{'id': 'c5', 'type': 'function',
                             'function': {'name': self.TOOL}}]},
            {'role': 'tool', 'name': self.TOOL, 'tool_call_id': 'c5',
             'content': 'Not able to perform this action now please try later'},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL])])
        self.assertEqual(
            unrun, [self.TOOL],
            "a tool whose own result says it could NOT perform the action "
            "must be reported UNRUN — counting it as executed lets the action "
            "advance on work that never happened")

    def test_companion_app_missing_is_not_execution(self):
        """The sibling failure return of the same tool (reuse_recipe.py:1927).

        Same branch, different cause: the companion app is not running, so
        nothing was done on the user's machine.  Pinned alongside its sibling
        so a fix keyed to only one observed string cannot leave the other
        counting as success.
        """
        msgs = [
            {'role': 'assistant',
             'tool_calls': [{'id': 'c6', 'type': 'function',
                             'function': {'name': self.TOOL}}]},
            {'role': 'tool', 'name': self.TOOL, 'tool_call_id': 'c6',
             'content': "I'm unable to perform this action since the Hevolve "
                        "A I Companion App is not running in your computer, "
                        "Open the companion app & try again"},
        ]
        unrun = self.rr._reuse_fabricated_tools(
            self.key, 1, _GroupChat(msgs), [_Agent([self.TOOL])])
        self.assertEqual(
            unrun, [self.TOOL],
            "'companion app is not running' means nothing ran on the user's "
            "machine — it must not clear the action's named tool")

    def test_sentinel_has_one_home(self):
        """The minter and the reader must share ONE sentinel definition."""
        import inspect
        from hartos import helper
        src = inspect.getsource(helper)
        self.assertNotIn(
            '"Placeholder response for historical tool call"', src,
            "helper.py must import the sentinel from core.constants, not "
            "carry its own literal — the fabrication gate keys on this exact "
            "string, so two copies can silently drift apart")
        self.assertTrue(
            HISTORICAL_TOOL_PLACEHOLDER.strip(),
            "the shared sentinel must be a non-empty string")


if __name__ == '__main__':
    unittest.main()
