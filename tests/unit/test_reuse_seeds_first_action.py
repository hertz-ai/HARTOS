"""Guard: the reuse loop COMMANDS the first action, it does not rely on the
user's phrasing to imply it.

Live root cause 2026-09-05 (Trading reuse 33204307184, installed build, measured
from llm_outbound.jsonl over a full 467s drive):

  39 autogen.reuse calls
   0  carried "Perform this action -> Action #N:"   <- the execution command
  30  carried the recipe (tool_name / google_search) in the SYSTEM prompt

`_build_reuse_action_message` has exactly ONE caller — `_advance_or_steer` —
so it only ever runs when advancing to the NEXT action.  Actions 2..N are
commanded explicitly; action 1 never is.  The loop opened with the user's raw
text ("Run the trading analysis now.") and the group chat simply discussed it:
google_search executed 0x, `Retrieved current_action_id: 1` 44x, no advance,
7.8 minutes, no outcome.

It is a closed loop — no action-1 command -> nothing executes -> nothing
completes -> no advance -> `_build_reuse_action_message` never runs.

Why it hid for so long: agents whose user text HAPPENS to imply action 1
(18088688973 "what is Tokyo's population" -> google_search) work fine, because
the raw question alone is enough to get the tool called.  Only agents whose
request is meta ("run my recipe") stall.

Fix: seed the opening turn with the CURRENT action's message built by the SAME
canonical `_build_reuse_action_message` the advance path uses — appended to the
user's message, never replacing it (the user's intent must survive, and the
working agents must not regress).

AST/text guard (no live llama needed).
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseSeedsFirstAction(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    def test_builder_is_called_for_the_opening_turn_too(self):
        # Before the fix the ONLY caller was _advance_or_steer, so action 1 was
        # never commanded.  There must now be at least two call sites: the
        # advance path AND the loop-entry seed.
        n = (self.src.count('_build_reuse_action_message(')
             - self.src.count('def _build_reuse_action_message('))
        self.assertGreaterEqual(
            n, 2,
            "action 1 is never commanded when the builder's only caller is the "
            "advance path — the opening turn must seed the current action too")

    def test_seed_does_not_replace_the_user_message(self):
        # The user's own words must still reach the group chat; the action
        # directive is ADDITIVE.  Regression risk: agents like 18088688973
        # succeed today purely off the user's phrasing.
        self.assertIn('_reuse_seed_message', self.src,
                      "the opening seed must be built through a named helper "
                      "so it is greppable and testable")
        helper = next(
            (n for n in ast.walk(self.tree)
             if isinstance(n, ast.FunctionDef) and n.name == '_reuse_seed_message'),
            None)
        self.assertIsNotNone(helper, "_reuse_seed_message must exist")
        called = {
            n.func.id for n in ast.walk(helper)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn(
            '_build_reuse_action_message', called,
            "the seed must reuse the canonical action-message builder, not "
            "hand-roll a second format (that is how the 5-way drift started)")

    def test_seed_helper_defined_once(self):
        self.assertEqual(self.src.count('def _reuse_seed_message('), 1,
                         "one seed helper, no parallel copy")

    def test_seed_is_failsafe(self):
        # A missing/!malformed recipe must NOT kill the turn — fall back to the
        # bare user message, which is exactly today's behaviour.
        helper_src = self.src.split('def _reuse_seed_message(', 1)[1]
        helper_src = helper_src.split('\ndef ', 1)[0]
        self.assertIn('except Exception', helper_src,
                      "seeding must never raise into the reuse turn; on any "
                      "failure fall back to the user message unchanged")


if __name__ == '__main__':
    unittest.main()
