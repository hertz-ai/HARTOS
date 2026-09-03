"""Guards for _reset_reuse_guard_state (reuse fabrication-gate budget reset).

The one-shot re-steer budget in hartos.reuse_recipe._reuse_resteer_counts is
keyed (user_prompt, action_id) and was never cleared, so it was spent for the
process lifetime: a later session re-running the same agent got no
fabrication protection.  create_agents_for_user now resets it per session.

Two guards:
  * behaviour — the helper clears exactly that prompt's budget entries and
    snapshot, leaves other prompts alone, and tolerates an empty state
    (RED before the helper existed: AttributeError);
  * wiring — an AST check that create_agents_for_user actually calls it
    (RED if the call is dropped: the budget silently goes back to
    process-lifetime with every unit test still green).
"""
import ast
import os
import unittest

_REUSE_SRC = os.path.join(os.path.dirname(__file__), '..', '..',
                          'hartos', 'reuse_recipe.py')


class ResetHelperBehaviour(unittest.TestCase):
    def setUp(self):
        try:
            import hartos.reuse_recipe as rr
        except Exception as e:  # heavy import surface (autogen/flask)
            self.skipTest(f'hartos.reuse_recipe not importable here: {e}')
        self.rr = rr

    def test_reset_clears_only_that_prompt(self):
        rr = self.rr
        rr._reuse_resteer_counts[('u1_p1', 1)] = 1
        rr._reuse_resteer_counts[('u1_p1', 2)] = 1
        rr._reuse_resteer_counts[('u2_p9', 1)] = 1
        rr._reuse_msg_snapshot['u1_p1'] = [{'role': 'user', 'content': 'x'}]
        rr._reuse_msg_snapshot['u2_p9'] = [{'role': 'user', 'content': 'y'}]
        try:
            rr._reset_reuse_guard_state('u1_p1')
            self.assertNotIn(('u1_p1', 1), rr._reuse_resteer_counts)
            self.assertNotIn(('u1_p1', 2), rr._reuse_resteer_counts)
            self.assertIn(('u2_p9', 1), rr._reuse_resteer_counts)
            self.assertNotIn('u1_p1', rr._reuse_msg_snapshot)
            self.assertIn('u2_p9', rr._reuse_msg_snapshot)
        finally:
            rr._reuse_resteer_counts.pop(('u2_p9', 1), None)
            rr._reuse_msg_snapshot.pop('u2_p9', None)

    def test_reset_is_noop_when_nothing_recorded(self):
        self.rr._reset_reuse_guard_state('nobody_here')  # must not raise


class ResetIsWiredIntoSessionCreation(unittest.TestCase):
    def test_create_agents_for_user_calls_reset(self):
        with open(_REUSE_SRC, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'create_agents_for_user')
        calls = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertIn('_reset_reuse_guard_state', calls)


if __name__ == '__main__':
    unittest.main()
