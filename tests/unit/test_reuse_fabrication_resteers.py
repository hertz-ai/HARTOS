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

    def test_advance_and_steer_have_exactly_one_call_site_each(self):
        # The advance-and-steer rule lives in ONE helper, _advance_or_steer.
        # Five sites used to inline it (w1-completed / w1 / w1-regex / w2 /
        # w2-regex), so the fabrication fix had to be applied five times and
        # the [HALLUCINATION?] check four.  Enforce the single home: exactly
        # one caller of each primitive, and it is the helper.
        n_advance = (self.src.count('_advance_reuse_action(')
                     - self.src.count('def _advance_reuse_action('))
        n_steer = (self.src.count('_reuse_fab_steer_message(')
                   - self.src.count('def _reuse_fab_steer_message('))
        self.assertEqual(
            n_advance, 1,
            f"_advance_reuse_action must have exactly ONE caller "
            f"(_advance_or_steer); found {n_advance} — an inline copy is back")
        self.assertEqual(
            n_steer, 1,
            f"_reuse_fab_steer_message must have exactly ONE caller "
            f"(_advance_or_steer); found {n_steer} — an inline copy is back")

    def test_helper_is_the_only_advance_caller(self):
        # Prove by AST that the one call sits inside _advance_or_steer, not
        # that it merely happens to be a count of one somewhere in the file.
        helper = next(
            (n for n in ast.walk(self.tree)
             if isinstance(n, ast.FunctionDef) and n.name == '_advance_or_steer'),
            None)
        self.assertIsNotNone(helper, "_advance_or_steer helper must exist")
        called = {
            n.func.id for n in ast.walk(helper)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn('_advance_reuse_action', called)
        self.assertIn('_reuse_fab_steer_message', called)

    def test_hallucination_check_is_not_duplicated(self):
        # Four sites each had their own copy of this warning.
        self.assertEqual(
            self.src.count('[HALLUCINATION?] LLM claims action_id='), 1,
            "the claimed-vs-assigned action check belongs in the one helper")

    def test_location_named_locals_are_gone(self):
        # The user's own review point: a local named after the loop it sits
        # in (_w2_current, _rc_next, _next2, _steer2) tells a reader nothing
        # about the value it holds.
        import re
        code = re.sub(r'""".*?"""', '', self.src, flags=re.S)  # drop docstrings
        for dead in ('_w2_current', '_w2_ledger', '_w2_task', '_rc_next',
                     '_rc_ok', '_rc_steer', '_next2', '_ok2', '_steer2'):
            self.assertNotIn(
                dead, code,
                f"{dead!r} names a code location, not a value — it must not "
                f"come back")

    def test_session_reset_clears_pending(self):
        self.assertIn('for _k in [k for k in _reuse_fab_pending if k[0] == user_prompt]',
                      self.src,
                      "a stale pending refusal must not steer the next session "
                      "about a tool it already ran")


if __name__ == '__main__':
    unittest.main()
