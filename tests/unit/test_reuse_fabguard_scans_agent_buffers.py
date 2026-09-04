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
        # Must key on the tool_call's function name, never "any tool ran".
        self.assertIn("((tc or {}).get('function') or {}).get('name')", self.src,
                      "executed detection must key on the SPECIFIC function "
                      "name so an unrelated tool cannot mark a named tool run")

    def test_detector_defined_once(self):
        self.assertEqual(self.src.count('def _reuse_fabricated_tools('), 1,
                         "one fabrication detector, no parallel copy")


if __name__ == '__main__':
    unittest.main()
