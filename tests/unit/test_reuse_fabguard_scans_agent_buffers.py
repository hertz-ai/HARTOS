"""Guard: the fabrication gate (_reuse_fabricated_tools) detects a tool as
executed if its call/result appears in ANY agent's _oai_messages buffer, not
only in group_chat.messages.

Live root cause 2026-09-05 (Trading reuse 33204307184, installed build):
google_search made a real HTTP 200 (primp log) and produced a role=='tool'
result, yet the guard logged executed=[]; unrun=['google_search'] and printed
[FABRICATED-COMPLETE].  Cause: the guard scanned only group_chat.messages,
but in this reuse flow the assistant tool_call + tool result are recorded in
the agents' pairwise _oai_messages (the same store the #725 sync reads),
NOT in the hooked group log.  A blind guard cannot tell a real completion
from a fabricated one — it "held once then advanced" regardless.

Fix: scan group_chat.messages AND every agent's _oai_messages buffers for the
role=='tool' result / assistant tool_calls.  Still keyed on the SPECIFIC
function name (never "any tool ran"), so the revwarm5407 false-positive
(an unrelated memory tool marking a never-run tool executed) cannot recur.

AST/text guard (no live llama needed).
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseFabGuardScansAgentBuffers(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    def test_scans_agent_oai_messages(self):
        self.assertIn("getattr(ag, '_oai_messages', None)", self.src,
                      "the fab-guard executed-detection must read each agent's "
                      "_oai_messages buffer (where tool activity actually lands "
                      "in the reuse flow), not only group_chat.messages")

    def test_still_scans_group_chat(self):
        self.assertIn("getattr(group_chat, 'messages', None)", self.src,
                      "the group-chat log must still be scanned (union, not "
                      "replacement)")

    def test_still_keyed_on_specific_function_name(self):
        """Never "any tool ran" — the executed set is keyed by tool NAME.

        Re-pointed 2026-09-06.  This used to pin the tool_calls expression
        `((tc or {}).get('function') or {}).get('name')`, but that line was
        never what enforced this property, and it has been REMOVED: counting a
        bare `tool_calls` entry meant counting the model's PROPOSAL as an
        execution (1,200 proposals for one tool against 1 real body entry),
        which is what made the gate unable to fail at all.  See
        test_fab_guard_counts_real_execution.py.

        The property itself — an unrelated tool cannot clear a named one —
        lives in the name-keyed membership test below, which the new code
        keeps and in fact tightens (fewer things can enter `executed`).
        Behavioural proof of the revwarm5407 case is in
        FabGuardCountsRealExecution.test_unrelated_tool_does_not_clear.
        """
        self.assertIn("unrun = [n for n in referenced if n not in executed]",
                      self.src,
                      "executed detection must key on the SPECIFIC function "
                      "name so an unrelated tool cannot mark a named tool run")
        # Re-pointed 2026-09-09.  The old anchor was the literal
        # `executed.add(m.get('name'))`, which 0c9159b46 ("fabrication gate
        # resolves the tool by call id, not by agent name") legitimately
        # replaced with `_record_result(...)` resolving the name through the
        # call-id map.  Measured that day: the string is absent at HEAD and at
        # every revision since that commit, so this assertion had been failing
        # on a REFACTOR, not on a regression — the property it protects still
        # holds.  Anchor on the property instead: every write into `executed`
        # happens inside _record_result, the one place that has a real result
        # in hand.
        tree = ast.parse(self.src)
        adds_outside = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name != '_record_result':
                continue
            inside = {id(n) for n in ast.walk(fn)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == 'add'
                        and isinstance(f.value, ast.Name)
                        and f.value.id == 'executed'
                        and id(node) not in inside):
                    adds_outside.append(node.lineno)
        self.assertTrue(
            any(isinstance(f, ast.FunctionDef) and f.name == '_record_result'
                for f in ast.walk(tree)),
            "_record_result is where a tool RESULT becomes an execution; if it "
            "was renamed, re-point this anchor rather than dropping the check")
        self.assertEqual(
            adds_outside, [],
            "something outside _record_result writes into `executed` (lines "
            + ', '.join(str(i) for i in adds_outside)
            + ") — only a real tool RESULT may mark a tool as run")

    def test_proposals_are_not_counted_as_execution(self):
        """The removed line must stay removed.

        A `tool_calls` entry is the model asking for a call.  Counting it as
        execution is what let nine fabricated 'completed' verdicts advance on
        2026-09-06 (agent 89555447799, actions 16..24).
        """
        # Re-pointed 2026-09-09, same reason as the anchor above.  The text
        # `((tc or {}).get('function') or {}).get('name')` is BACK in the file
        # (0c9159b46) and legitimately so: it builds `_call_fn`, a call-id ->
        # function-name MAP that lets a RESULT be resolved to its tool.  A map
        # is not a count.  Measured: present at HEAD and at every revision
        # back through that commit, so this assertion has been failing on the
        # refactor since 2026-09-06 while the property held.
        #
        # The property is: reading `tool_calls` may build a lookup, but must
        # not add to `executed`.  Assert exactly that.
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = ast.dump(node.iter)
            if "'tool_calls'" not in it:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                f = inner.func
                self.assertFalse(
                    isinstance(f, ast.Attribute) and f.attr == 'add'
                    and isinstance(f.value, ast.Name) and f.value.id == 'executed',
                    f"line {inner.lineno}: a proposed tool_call is being added "
                    "to the executed set — that is the model asking, not the "
                    "tool running (nine fabricated 'completed' verdicts "
                    "advanced this way on 2026-09-06)")

    def test_detector_defined_once(self):
        self.assertEqual(self.src.count('def _reuse_fabricated_tools('), 1,
                         "one fabrication detector, no parallel copy")


if __name__ == '__main__':
    unittest.main()
