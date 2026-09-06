"""Guard: subtasks are persisted where the verdict is actually observed.

Live root cause, measured 2026-09-06 18:07:44-18:33:10 (agent 89555447799,
25-min reuse drive on the installed build).  The turn ended by returning raw
control JSON to the user:

    {"status": "requires_breakdown", "action": "Action #9: Open the LinkedIn
     web interface...", "subtasks": [9.1, 9.2, 9.3]}

and action 9 span 24 times in the last three minutes, ~6s apart, emitting the
same line: "[BREAKDOWN] action 9 reported requires_breakdown but the ledger
holds no pending subtask".

Why the ledger was empty.  `add_subtasks` is called from exactly one place —
`state_transition` (reuse_recipe.py:2667) — and `state_transition` is
autogen's SPEAKER-SELECTION callback.  Selection runs to choose who speaks
NEXT, so it cannot run on a round's final message; the `requires_breakdown`
verdict IS that final message.  The producer therefore never sees the verdict
that carries the subtasks, while the consumer (the `[BREAKDOWN]` block in the
w1 loop) sees it on every iteration and finds nothing to work.

Measured interleaving over the drive window:

    state_transition JSON-branch entries     11   (all error/pending, 0 breakdown)
    state_transition tool-route entries     118
    state_transition @mention entries        15
    [BREAKDOWN] consumer entries             27
    subtasks persisted                        0
    add_subtasks exceptions                   0

and across the final 24 consecutive [BREAKDOWN] iterations, ZERO
state_transition events of any kind — it is not merely taking a different
branch, it is not being called at all.

The designed flow is documented in the code itself
(reuse_recipe.py:3567-3569, mirroring create_recipe.py:4503-4520):

    add_subtasks() -> get_pending_subtasks() -> work each -> parent

All four steps must live where the verdict is readable.  This test pins that
co-location: whichever function calls `get_pending_subtasks` must also call
`add_subtasks`, so a future refactor cannot re-separate the producer from the
consumer and silently reintroduce a ledger nobody fills.

It is a structural guard on purpose.  `state_transition` and the w1 loop are
closures inside a ~3000-line factory that constructs a dozen live agents; a
behavioural test would have to stand up that whole world, and the invariant
being protected — "these two calls are in the same scope" — is exactly what
the defect violated.
"""
import ast
import os
import unittest

_SRC = os.path.join(os.path.dirname(__file__), '..', '..',
                    'hartos', 'reuse_recipe.py')


def _calls_in(node):
    """Every called name inside `node`, including nested closures."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _functions(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


class BreakdownPersistsWhereItReads(unittest.TestCase):

    def setUp(self):
        with open(_SRC, encoding='utf-8') as fh:
            self.tree = ast.parse(fh.read())

    def test_the_consumer_of_subtasks_is_also_their_producer(self):
        """add_subtasks must be reachable from the scope that reads them."""
        readers = [f for f in _functions(self.tree)
                   if 'get_pending_subtasks' in _calls_in(f)]
        self.assertTrue(
            readers,
            'no function calls get_pending_subtasks — the breakdown consumer '
            'has gone missing entirely')

        # Innermost reader wins: ast.walk yields enclosing functions too, and
        # an outer function trivially "contains" both calls while the inner
        # closures stay separated — which is the exact defect.  Score by the
        # smallest reader that reads.
        readers.sort(key=lambda f: len(list(ast.walk(f))))
        innermost = readers[0]
        # Either spelling counts as persisting: `add_subtasks_to_ledger` is the
        # canonical module-level helper both pipelines import, `add_subtasks`
        # the ledger method reuse used to reach for.  The invariant under test
        # is co-location, not which name — pinning one name would fail the day
        # someone correctly migrates to the other.
        self.assertTrue(
            {'add_subtasks', 'add_subtasks_to_ledger'} & _calls_in(innermost),
            "the innermost scope that calls get_pending_subtasks (%s) does "
            "NOT call add_subtasks.  That is the 2026-09-06 defect: the only "
            "add_subtasks call lived in state_transition, autogen's SPEAKER "
            "SELECTOR, which never runs on a round's final message — and the "
            "requires_breakdown verdict carrying the subtasks IS that final "
            "message.  Action 9 span 24 times on an empty ledger."
            % innermost.name)

    def test_add_subtasks_is_not_called_from_the_speaker_selector_alone(self):
        """A write that only the selector performs is a write that never runs.

        Keeping this separate from the test above so the failure message says
        WHICH half is wrong: producer missing from the consumer's scope, or
        producer still stranded in the selector.

        Scored on the INNERMOST writer, for the same reason the co-location
        test is: ``ast.walk`` yields enclosing functions too, so the ~3000-line
        factory that lexically contains ``state_transition`` counts as a
        "writer" and makes a naive selector-only check pass while every real
        write is still stranded in the selector.  I wrote that vacuous version
        first and it passed against the known-broken tree — the exact
        feedback_vacuous_guards shape.
        """
        writers = [f for f in _functions(self.tree)
                   if any(n in _calls_in(f)
                          for n in ('add_subtasks', 'add_subtasks_to_ledger'))]
        self.assertTrue(writers, 'subtasks are persisted from nowhere')
        writers.sort(key=lambda f: len(list(ast.walk(f))))
        innermost = writers[0]
        self.assertFalse(
            innermost.name.startswith('state_transition'),
            'the innermost scope that persists subtasks is %s — a speaker '
            'selector.  Selection picks who talks NEXT, so it never runs on '
            'the terminal verdict, and the subtasks that verdict carries are '
            'dropped.  Measured 2026-09-06: 24 consecutive [BREAKDOWN] '
            'iterations with ZERO state_transition events of any kind.'
            % innermost.name)

    def test_reuse_uses_the_canonical_ledger_helper_like_create_does(self):
        """One API for one concern — create_recipe already picked it.

        ``add_subtasks_to_ledger`` is the module-level helper both pipelines
        import (reuse_recipe.py:181, create_recipe.py:272).  create_recipe
        CALLS it (:2556, :4510) and reads back with get_pending_subtasks
        (:4518) in the same scope.  reuse_recipe imported it and never called
        it, reaching for ``ledger.add_subtasks`` instead — a second way to say
        the same thing, which is how the two pipelines drift.
        """
        called = set()
        for f in _functions(self.tree):
            called |= _calls_in(f)
        self.assertIn(
            'add_subtasks_to_ledger', called,
            'reuse_recipe imports add_subtasks_to_ledger (:181) but never '
            'calls it — the import is dead and the persistence goes through a '
            'different API than create_recipe uses for the identical job')


if __name__ == '__main__':
    unittest.main()
