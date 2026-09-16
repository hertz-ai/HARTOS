"""A failed reuse advance must be VISIBLE, with its exception.

THE LIVE FAILURE THIS ENCODES (2026-09-11, installed build, session
6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304, window 01:49:12-02:09:58,
hevolveai subprocess lines excluded):

    "reuse-w1-completed ... for action 1 — advancing"        20
    "[REUSE] Action N TERMINATED, advancing"                   0
    "[REUSE] Cannot advance action ... not TERMINATED"         0
    "already TERMINATED (idempotent)"                          0
    "[FABRICATED-COMPLETE] refusing to advance"                0
    "All N actions completed"                                  0
    "[SUBTASK-HOLD]"                                           0

`[REUSE] Action N TERMINATED, advancing` is the statement IMMEDIATELY BEFORE
the single write that moves the pointer (`user_tasks[...].current_action =
next_id`), and `[SUBTASK-HOLD]` is the only early return in `_advance_or_steer`.
All seven markers absent means the advance neither completed, nor refused, nor
returned -- it RAISED, and the caller's `except` caught it.

That except logged at DEBUG.  The same log holds 45,599 lines since the restart
and ZERO matching "- DEBUG - ", so the one message explaining why the walk
stopped advancing has never reached production on this build.  The observable
result: action 1 executed its tool for real (FAB-GUARD unrun=[]) and the walk
then spun on action 1 for twenty minutes, reaching 1 of the agent's 9 actions.

This is the same shape as the attach-branch gap fixed in 822b68630 -- a failure
path written at a level this build does not capture -- which is why the fix is
the level and exc_info, not new machinery.

WHAT THIS GUARD DOES NOT CLAIM: it does not assert the walk advances, and it
does not name the underlying exception.  The exception is still UNKNOWN; making
it visible is the point.  Do not read a green run here as "D66 fixed" (#831).
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src():
    return io.open(os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py'),
                   encoding='utf-8', errors='replace').read()


def _completion_advance_handler(src):
    """The `except` that wraps the robust completion-advance call.

    Anchored on the exception NAME (`_rc_err`), which is unique to this handler,
    rather than on a line number or a character budget -- the earlier
    `_empty_branch` helper in this suite went vacuous exactly once by using a
    char budget that the block outgrew, and returned '' while every assertion
    against it still passed.
    """
    m = re.search(r'except\s+Exception\s+as\s+_rc_err\s*:\s*\n(.*?)(?=\n\s{12}#\s|\n\s{12}[a-zA-Z_]+\s*=|\Z)',
                  src, re.S)
    return m.group(1) if m else ''


class TestAdvanceFailureReachesProduction(unittest.TestCase):
    """RED until the swallowed advance failure is logged where prod can see it."""

    def test_the_handler_exists(self):
        block = _completion_advance_handler(_src())
        self.assertTrue(block, 'the _rc_err handler is gone; re-point this guard')
        # Non-vacuity: a capture that lost the log call makes the rest pass
        # against text that proves nothing.
        self.assertIn(
            'logger.', block,
            'the capture no longer contains the log call, so the assertions '
            'below would pass vacuously -- re-point _completion_advance_handler')

    def test_it_does_not_log_at_debug(self):
        block = _completion_advance_handler(_src())
        self.assertNotIn(
            'logger.debug', block,
            'the advance-failure handler logs at DEBUG, and this build captured '
            '0 of 45,599 lines at DEBUG since restart -- so when the ONLY '
            'pointer-moving call raises, nothing says so and the walk spins on '
            'one action forever (measured 20x on action 1, 2026-09-11)')

    def test_it_carries_the_exception(self):
        """Absences forced this investigation; the next reader gets the traceback."""
        block = _completion_advance_handler(_src())
        self.assertIn(
            'exc_info', block,
            'the handler logs the failure without exc_info, so the actual '
            'exception still has to be inferred from which log lines are '
            'ABSENT -- which is exactly the work #831 had to do by hand')

    def test_it_names_the_session(self):
        """Two other agents drove this same log concurrently on 2026-09-11."""
        block = _completion_advance_handler(_src())
        self.assertTrue(
            re.search(r'for session: \{user_prompt\}', block),
            'the advance-failure line does not name its session; on a box with '
            'daemon agents and a second user driving reuse, an unattributed '
            'failure line cannot be tied to the walk under test')


if __name__ == '__main__':
    unittest.main()
