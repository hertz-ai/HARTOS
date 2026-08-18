"""P1: the WAMP RPC helper is ONE implementation, and it fails LOUDLY.

THE DUPLICATION
───────────────
`subscribe_and_return` was 88 lines copied verbatim into create_recipe.py:815 and
reuse_recipe.py:338, each called twice from its own file. Two live copies of one
concept: every fix had to be made twice, and nothing said when you missed one.

THE DRIFT IT HAD ALREADY ACCUMULATED
────────────────────────────────────
The copies were byte-identical except for ONE statement's position:

    create_recipe             reuse_recipe
    actual_timeout = t/1000+5     try:
    try:                              await component.start()
        await component.start()       actual_timeout = t/1000+5

That matters because the function ends in a broad `except Exception: return None`.
In reuse_recipe a non-numeric `time` raised TypeError INSIDE the guarded block, so
it was caught, logged as an RPC error, and returned None — the caller could not
distinguish "you passed a bad argument" from "the remote call failed". In
create_recipe the same mistake surfaced loudly.

The drift was itself an instance of the silent-failure class, so the collapse kept
create_recipe's placement. These tests pin that decision so a future edit cannot
quietly move the arithmetic back inside the try.
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def _fn(path, name):
    tree = ast.parse(open(os.path.join(REPO, path), encoding='utf-8',
                          errors='replace').read())
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


class ThereIsExactlyOneImplementation(unittest.TestCase):

    def test_the_pipeline_files_no_longer_define_it(self):
        """create_recipe and reuse_recipe called it twice each from their own
        88-line copy. Both now call the canonical one."""
        for path in ('create_recipe.py', 'reuse_recipe.py'):
            self.assertIsNone(
                _fn(path, 'subscribe_and_return'),
                "%s defines subscribe_and_return again — the 88-line duplicate is "
                "back, and it will drift from helper.py's copy exactly as before"
                % path)

    def test_helper_owns_the_canonical_copy(self):
        n = _fn('helper.py', 'subscribe_and_return')
        self.assertIsNotNone(n, "the canonical implementation is missing from helper.py")
        self.assertGreater(n.end_lineno - n.lineno, 20,
                           "helper.py's copy looks like a stub, not the real one")

    def test_every_call_site_goes_through_helper(self):
        for path in ('create_recipe.py', 'reuse_recipe.py'):
            src = open(os.path.join(REPO, path), encoding='utf-8',
                       errors='replace').read()
            bare = src.count('await subscribe_and_return(')
            routed = src.count('helper_fun.subscribe_and_return(')
            self.assertEqual(0, bare,
                             "%s still calls a local subscribe_and_return" % path)
            self.assertGreater(routed, 0,
                               "%s no longer calls the helper at all — did a call "
                               "site get dropped?" % path)


class TheTimeoutArithmeticStaysOutsideTheTry(unittest.TestCase):
    """The drift decision, pinned. Inside the try, a bad argument becomes a silent
    None because of the broad `except Exception: return None` below it."""

    def _canonical(self):
        n = _fn('helper.py', 'subscribe_and_return')
        self.assertIsNotNone(n)
        return n

    def test_actual_timeout_is_assigned_before_the_try(self):
        n = self._canonical()
        assign_line = try_line = None
        for node in ast.walk(n):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == 'actual_timeout':
                        assign_line = node.lineno
            elif isinstance(node, ast.Try) and try_line is None:
                # the OUTER try that wraps component.start() + wait_for
                if any('component.start' in ast.unparse(s) for s in node.body):
                    try_line = node.lineno
        self.assertIsNotNone(assign_line, "actual_timeout is no longer assigned")
        self.assertIsNotNone(try_line, "the guarded try block is gone")
        self.assertLess(
            assign_line, try_line,
            "actual_timeout is assigned INSIDE the try (line %s vs try at %s). The "
            "broad `except Exception: return None` below turns a bad `time` argument "
            "into a silent None, so the caller cannot tell a programming error from "
            "a failed RPC. That is the exact drift this consolidation removed."
            % (assign_line, try_line))

    def test_the_broad_except_still_returns_None(self):
        """Documents WHY the placement matters. If this ever stops being true the
        rule above can be relaxed — but not before."""
        n = self._canonical()
        src = ast.unparse(n)
        self.assertIn('except Exception', src)
        self.assertIn('return None', src)


if __name__ == '__main__':
    unittest.main()
