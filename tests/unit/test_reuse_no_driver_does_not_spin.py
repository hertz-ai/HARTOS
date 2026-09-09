"""A reuse action nothing can drive must END the turn, never re-loop unchanged.

MEASURED LIVE 2026-09-09, agent 33323830039 action 2 of 4, installed build.
All 13 `inside reuse while1` passes landed in a single 10-MILLISECOND burst,
07:27:31,833 -> ,843, and the whole per-action allowance was gone:

    07:27:31,833  inside reuse while1      <- x13, within 10 ms
    ...
    07:27:31,843  [REUSE-ROUNDS] while1 action 2/4 used its 12 rounds without
                  completing — ending turn (turn spend 12/48)

Outbound calls in that phase (llm_outbound.jsonl, producer autogen.reuse): 2.
The three real conversational rounds (07:27:26, :29, :31.4) happened INSIDE one
initiate_chat, not in those passes.  So ~11 iterations each spent an allowance
unit while doing no model work, and the round cap is the only thing that ended
an otherwise unbounded spin.

WHICH PATH, proven by the absence of every other branch's marker.  Every line
in the burst is a bare `inside reuse while1` — zero occurrences of
'GOT can_perform_without_user_input as true', 'Message directed to agent',
'continuing since @user not in last message', 'WE have some indexx error here',
or '@user in last message'.  With the last message being the StatusVerifier
verdict {'status':'pending','action':'Action #2: cd C:\\Users\\sathi\\
Documents','action_id':2,...} that leaves exactly one path through while1:

  - completion-advance   status != 'completed'          -> skip
  - breakdown            status != 'requires_breakdown'  -> skip
  - UNDER-REPORT escape  'pending' qualifies, but the branch ALSO requires
                         _reuse_action_is_autonomous(...) -> False -> skip
  - TERMINATE gate       last speaker is StatusVerifier  -> skip
  - the only initiate_chat driver is gated on the SAME predicate -> not called
  - `elif '@user' not in content_lower:` -> no agent mention matches -> the
    branch body does nothing: no continue, no break.  Control falls off the end
    of the loop body and the next pass repeats it with nothing changed.

So for a NON-autonomous action whose last message is a verdict that is neither
completed nor requires_breakdown, while1 has NO handler at all: it cannot
advance, cannot ask, and cannot exit.  That hole is independent of whether the
autonomy flag is correct — a loop iteration that changes nothing must not cost
an allowance unit and then run again identically.

THE FIX REUSES THE EXIT THAT IS ALREADY THERE: break to the post-loop
extractor (#798), the same exit the TERMINATE/IndexError path takes, which
already synthesises the user-facing reply (observed live at 07:27:31,844,
'[SYNTHESIS] reply would be raw control JSON — asking for the user-facing
answer').  No new mechanism, no change to the autonomy predicate, no parallel
path.

ANTI-VACUITY: the exit must be guarded by `not _reuse_action_is_autonomous`.
An AUTONOMOUS action legitimately falls through here — its next pass re-drives
the group through initiate_chat, which is real progress.  Breaking on that path
would truncate working agents, so the guard is the whole point.

    python -m pytest tests/unit/test_reuse_no_driver_does_not_spin.py \
        --noconftest -q
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..',
                   'hartos', 'reuse_recipe.py')


def _tree():
    with open(SRC, encoding='utf-8') as fh:
        return ast.parse(fh.read()), fh


def _at_user_branches(tree):
    """Every `if/elif` whose test mentions the '@user' needle.

    That test is what selects "this message is not addressed to the user",
    i.e. the branch the live spin fell through.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Constant) and sub.value == '@user':
                out.append(node)
                break
    return out


def _branch_body(branch):
    """Only the elif's OWN body — never its ``orelse``.

    The ``else:`` arm of this same If is the pre-existing '@user IS in the last
    message' delivery path, and it legitimately ends with ``break``.  An earlier
    draft of this file walked the whole If node and flagged that break as
    unguarded, which was the test being wrong, not the code.
    """
    return list(branch.body)


def _guarded_break_nodes(branch):
    """`break` statements inside a `not _reuse_action_is_autonomous(...)` test.

    Returns the Break nodes themselves so the anti-vacuity check can compare
    identity rather than line numbers.
    """
    found = []
    for stmt in _branch_body(branch):
        for node in ast.walk(stmt):
            if not isinstance(node, ast.If):
                continue
            t = node.test
            if not (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)):
                continue
            if not [c for c in ast.walk(t.operand)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Name)
                    and c.func.id == '_reuse_action_is_autonomous']:
                continue
            found.extend(s for s in ast.walk(node) if isinstance(s, ast.Break))
    return found


def _all_breaks(branch):
    """Every `break` in the elif's own body (its ``orelse`` is excluded)."""
    return [s for stmt in _branch_body(branch)
            for s in ast.walk(stmt) if isinstance(s, ast.Break)]


class ReuseNoDriverDoesNotSpin(unittest.TestCase):

    def setUp(self):
        with open(SRC, encoding='utf-8') as fh:
            self.src = fh.read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    def test_at_user_branch_exists(self):
        """Anchor check: if this branch is renamed, re-point rather than drop."""
        self.assertTrue(
            _at_user_branches(self.tree),
            "no branch tests the '@user' needle any more — the spin path was "
            "restructured; re-point this guard at its replacement instead of "
            "deleting it")

    def test_non_autonomous_fallthrough_ends_the_turn(self):
        """THE DEFECT. RED before the fix.

        At least one '@user' branch must carry a `break` guarded by
        `not _reuse_action_is_autonomous(...)`, so an action nothing can drive
        ends the turn instead of re-looping unchanged.
        """
        hits = [n for b in _at_user_branches(self.tree)
                for n in _guarded_break_nodes(b)]
        self.assertTrue(
            hits,
            "the '@user' branch can still fall through with no continue and no "
            "break: for a NON-autonomous action nothing changes between passes, "
            "so the loop spins until the round cap (measured live 2026-09-09: "
            "13 passes in 10 ms, 2 LLM calls, allowance gone). It must break to "
            "the post-loop extractor when _reuse_action_is_autonomous is False.")

    def test_the_exit_is_guarded_not_unconditional(self):
        """Anti-vacuity: an AUTONOMOUS action must still fall through.

        Its next pass re-drives the group via initiate_chat — real progress.
        An unguarded break here would truncate every working agent, which is
        exactly the force-stop the verification contract forbids.
        """
        for branch in _at_user_branches(self.tree):
            guarded = {id(n) for n in _guarded_break_nodes(branch)}
            for node in _all_breaks(branch):
                self.assertIn(
                    id(node), guarded,
                    f"line {node.lineno}: a break in the '@user' branch body is "
                    "not guarded by `not _reuse_action_is_autonomous(...)` — "
                    "that would end the turn for autonomous actions too, which "
                    "make real progress on their next pass")

    def test_predicate_is_the_existing_one(self):
        """Reuse the canonical autonomy predicate — do not add a second."""
        self.assertEqual(
            self.src.count('def _reuse_action_is_autonomous('), 1,
            'one autonomy predicate, no parallel copy')


if __name__ == '__main__':
    unittest.main()
