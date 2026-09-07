"""Guard: the reuse w1 loop advances through recipe actions on the verifier's
'completed' verdict, independent of the fragile ChatInstructor-TERMINATE-at-[-1]
gate.

Live root cause 2026-09-05 (Auto Research reuse 18088688973): action 1's verdict
was {'status':'completed','action_id':1} twice and "GOT COMPLETED FOR ACTION"
fired, yet the loop self-looped on action 1 (24x current_action_id:1) and actions
2-6 never ran — the ONLY advance trigger (reuse_recipe.py:3145) requires a
ChatInstructor 'TERMINATE' at messages[-1], which did not land.  The robust
completion-advance re-uses the SAME _advance_reuse_action, keyed on a verifier
'completed' verdict for the CURRENT action (action_id matched, one-advance-per-
action guarded).  AST guard, no live llama.
"""
import ast, os, unittest

SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _get_agent_response_body():
    src = open(SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_agent_response':
            return ast.get_source_segment(src, node) or ''
    return ''


class ReuseMultiActionAdvance(unittest.TestCase):
    def setUp(self):
        self.body = _get_agent_response_body()
        self.assertTrue(self.body, "get_agent_response not found")

    def test_robust_completed_advance_present(self):
        self.assertIn('reuse-w1-completed', self.body,
                      "loop must advance on a verifier 'completed' verdict, not "
                      "only on a ChatInstructor TERMINATE at [-1]")

    def test_action_id_matched_and_guarded(self):
        self.assertIn('_reuse_advanced_actions', self.body,
                      "must guard one-advance-per-action so a stale verdict "
                      "cannot double-advance")
        # Re-pointed 2026-09-06.  This asserted the literal "_rc_vj.get('action_id'",
        # a local that no longer exists: _advance_or_steer's consolidation replaced
        # five inlined copies (_rc_next/_next/_next2/_w2_next/_w2_next2 and their
        # _rc_vj twins) with one function taking claimed_action_id=, which owns the
        # [HALLUCINATION?] comparison.  The INTENT is unchanged — the verdict's
        # action_id must still be carried to the advance — so pin the surviving
        # expression instead of the deleted variable name.
        self.assertIn('claimed_action_id=', self.body,
                      "the verdict's action_id must be carried into the advance "
                      "so a verdict naming a different action is flagged")
        self.assertIn('"action_id"', self.body,
                      "claimed_action_id must be READ from the verdict json")

    def test_reuses_canonical_advance(self):
        # Also re-pointed: get_agent_response no longer calls _advance_reuse_action
        # directly — it goes through _advance_or_steer, the single home for the
        # advance-or-re-steer rule.  Pin the whole chain so a parallel path still
        # fails this guard: the body must call _advance_or_steer, and
        # _advance_or_steer must be the thing that calls _advance_reuse_action.
        self.assertIn('_advance_or_steer(', self.body,
                      "must reuse the canonical advance/steer fn, not a parallel path")
        src = open(SRC, encoding='utf-8').read()
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == '_advance_or_steer'), None)
        self.assertIsNotNone(fn, '_advance_or_steer not found')
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertIn('_advance_reuse_action', called,
                      "_advance_or_steer must delegate to the canonical "
                      "_advance_reuse_action rather than reimplementing the advance")


if __name__ == '__main__':
    unittest.main()
