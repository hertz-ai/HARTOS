"""Guard: a fabricated completion RE-STEERS the agent to actually run the
action's tool — it is never silently force-completed by a spent budget.

Live root cause 2026-09-05 (Trading reuse 33204307184, installed build):
the StatusVerifier claimed action 1 "completed" BEFORE its google_search had
run (the real HTTP call landed ~40s later).  The fabrication gate held exactly
once (`_reuse_resteer_counts.get(_rk, 0) < 1`) and then FELL THROUGH to
COMPLETED/TERMINATED — so the very next claim advanced an action whose tool had
still never executed.  Six actions advanced that way.  That is precisely the
"action force-completed by a stuck-loop guard or a nudge" the verification
contract forbids: the ledger says completed, the tool never ran.

Second half of the same defect (review 2026-09-03 #1): _advance_reuse_action
returns (None, False) for BOTH "fabricated" and "all actions done / state
error", so every caller treated a refusal as done and ended the turn with an
empty reply instead of steering the agent to run the tool.

Fix (both halves guarded here):
  A. the gate re-steers up to _REUSE_FAB_STEER_MAX (>1) times, recording the
     unrun tools each time, and only then advances — LOUDLY, at error level,
     naming the output as not tool-backed, so it can still never permanently
     stall;
  B. _reuse_fab_steer_message() pops that record so callers can tell a
     fabrication refusal from "all done", and EVERY _advance_reuse_action call
     site consults it and steers instead of returning an empty turn.

AST/text guard (no live llama needed).
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseFabricationResteers(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    # --- Fix A: bounded RE-STEER, not a one-shot budget then silent advance ---
    def test_steer_budget_is_more_than_one(self):
        self.assertIn('_REUSE_FAB_STEER_MAX', self.src,
                      "the re-steer budget must be a named constant")
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, 'id', None) == '_REUSE_FAB_STEER_MAX'
                            for t in node.targets)):
                self.assertIsInstance(node.value, ast.Constant)
                self.assertGreater(
                    node.value.value, 1,
                    "one hold then fail-open is what advanced a tool-less action "
                    "live on 2026-09-05; the agent needs real chances to run it")
                break
        else:
            self.fail("_REUSE_FAB_STEER_MAX assignment not found")

    def test_old_one_shot_budget_is_gone(self):
        self.assertNotIn("_reuse_resteer_counts.get(_rk, 0) < 1", self.src,
                         "the one-shot budget must not come back — it is what "
                         "let a fabricated completion advance on the 2nd claim")

    def test_gate_records_unrun_tools_for_the_caller(self):
        self.assertIn('_reuse_fab_pending[_rk] = list(_fab)', self.src,
                      "each refusal must record WHICH tools never ran so the "
                      "caller can name them when steering")

    def test_fail_open_is_loud_and_honest(self):
        # After the budget it still advances (never a permanent stall) but must
        # say so at error level and mark the output as not tool-backed.
        self.assertIn('NOT tool-backed', self.src,
                      "the post-budget advance must declare that the action's "
                      "output is not tool-backed, never advance silently")

    # --- Fix B: refusal is distinguishable and every caller re-steers ---
    def test_steer_message_helper_defined_once(self):
        self.assertEqual(self.src.count('def _reuse_fab_steer_message('), 1,
                         "one helper, no parallel copy")

    def test_helper_pops_the_pending_record(self):
        self.assertIn('_reuse_fab_pending.pop((user_prompt, current_action_id), None)',
                      self.src,
                      "the helper must CONSUME the refusal so one refusal "
                      "produces exactly one steer")

    def test_every_advance_call_site_consults_the_steer(self):
        # 5 real call sites (w1 completed / w1 / w1-regex / w2 / w2-regex).
        n_calls = self.src.count('_advance_reuse_action(') - self.src.count('def _advance_reuse_action(')
        n_steers = self.src.count('_reuse_fab_steer_message(') - self.src.count('def _reuse_fab_steer_message(')
        self.assertEqual(
            n_steers, n_calls,
            f"every _advance_reuse_action call site ({n_calls}) must consult "
            f"_reuse_fab_steer_message before ending the turn; found {n_steers}")

    def test_session_reset_clears_pending(self):
        self.assertIn('for _k in [k for k in _reuse_fab_pending if k[0] == user_prompt]',
                      self.src,
                      "a stale pending refusal must not steer the next session "
                      "about a tool it already ran")


if __name__ == '__main__':
    unittest.main()
