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
        self.assertIn("_rc_vj.get('action_id'", self.body,
                      "must match the verdict action_id to the current action")

    def test_reuses_canonical_advance(self):
        self.assertIn('_advance_reuse_action(', self.body,
                      "must reuse the canonical advance fn, not a parallel path")


if __name__ == '__main__':
    unittest.main()
