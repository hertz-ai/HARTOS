"""Guard: a completion verdict is found wherever it sits, not only at [-1].

Measured live 2026-09-07 09:13-09:19 on agent 30611960713 — the smallest
real agent in the population: ONE action, whose one tool
(`get_user_uploaded_file`) executed 2/2 successfully and whose FAB-GUARD
passed with `unrun=[]`.  It still never completed.

Both completion consumers read `group_chat.messages[-1]`:

  * reuse_recipe.py:~3795  fires only when messages[-1] is
    ChatInstructor/'TERMINATE', then reads the verdict from messages[-2];
  * the [UNDER-REPORTED] block read the verdict from messages[-1] directly.

The verdict is almost never last.  state_transition is autogen's
speaker-selection callback, so a steer lands after the verdict and buries it.
Across all 59 state_transition calls in that window, messages[-1] was a steer
EVERY time ("You should complete this task independently" x29, "Work on
subtask: ..." x20, "Perform this action -> ..." x6, user/goal x4) and never a
verdict.  StatusVerifier meanwhile emitted 6 'completed' verdicts, so
`GOT COMPLETED FOR ACTION` fired 0 times and the action looped on 1 for five
minutes.

`_reuse_latest_verdict` is the one reader both paths can share.  It is
READ-ONLY: callers still route through `_advance_or_steer`, so the fabrication
gate inside `_advance_reuse_action` ("canonical single point — EVERY advance
path calls this") still refuses and re-steers an action whose tool produced no
real result.  The action_id binding below is the regression guard: without it
a stale 'completed' for action N would advance action N+1, which is exactly
the "force-completed by a nudge" failure a6fd615e5 exists to prevent.

    python -m pytest tests/unit/test_reuse_verdict_off_tail.py -q
"""
import json
import unittest

from hartos.reuse_recipe import _reuse_latest_verdict


class _GC:
    """Minimal stand-in for the autogen GroupChat — only .messages is read."""

    def __init__(self, messages):
        self.messages = messages


def _verdict(status, action_id=None, action='get_user_uploaded_file'):
    body = {'status': status, 'action': action}
    if action_id is not None:
        body['action_id'] = action_id
    return {'role': 'assistant', 'name': 'StatusVerifier',
            'content': json.dumps(body)}


def _steer(text):
    return {'role': 'user', 'name': 'ChatInstructor', 'content': text}


# The real tail shape from the failing window, verdict first then steers.
_REAL_TAIL = [
    _steer('Perform this action -> Action #1:get_user_uploaded_file'),
    _verdict('completed', 1),
    _steer('You should complete this task independently. Feel free to make '
           'reasonable assumptions where necessary'),
    _steer('Work on subtask: Identify the most recent file metadata '
           'associated with the current conversation context.'),
]


class VerdictFoundOffTheTail(unittest.TestCase):

    def test_finds_a_completed_verdict_buried_under_steers(self):
        """THE REGRESSION. messages[-1] is a steer; the verdict is at [-3]."""
        gc = _GC(list(_REAL_TAIL))
        self.assertNotIn('status', gc.messages[-1]['content'],
                         'precondition: the last message must NOT be the verdict')
        found = _reuse_latest_verdict(gc, 1)
        self.assertIsInstance(found, dict,
                              'the verdict exists in history and must be found')
        self.assertEqual(found.get('status'), 'completed')

    def test_verdict_for_a_DIFFERENT_action_is_refused(self):
        """Stale-advance guard — the whole reason this is action-bound.

        A leftover 'completed' for action 1 must never satisfy action 2, or a
        nudge force-completes work that never ran.
        """
        gc = _GC([_verdict('completed', 1), _steer('You should ...')])
        self.assertIsNone(_reuse_latest_verdict(gc, 2))

    def test_subtask_id_matches_its_parent_action(self):
        """'1.1' is action 1's subtask — same action, not a different one."""
        gc = _GC([_verdict('completed', '1.1'), _steer('You should ...')])
        found = _reuse_latest_verdict(gc, 1)
        self.assertIsInstance(found, dict)
        self.assertEqual(found.get('status'), 'completed')

    def test_verdict_without_an_action_id_is_accepted(self):
        """The pipeline's own current_action is the authority when none named."""
        gc = _GC([_verdict('completed', None), _steer('You should ...')])
        self.assertIsInstance(_reuse_latest_verdict(gc, 1), dict)

    def test_newest_verdict_wins(self):
        """A later 'completed' supersedes an earlier 'pending' for the action."""
        gc = _GC([_verdict('pending', 1), _steer('...'),
                  _verdict('completed', 1), _steer('...')])
        self.assertEqual(_reuse_latest_verdict(gc, 1).get('status'), 'completed')

    def test_no_verdict_at_all_returns_none(self):
        """Prose-only history must not be coerced into a verdict."""
        gc = _GC([_steer('You should ...'), _steer('Work on subtask: ...')])
        self.assertIsNone(_reuse_latest_verdict(gc, 1))

    def test_empty_and_malformed_history_are_safe(self):
        """Never raise into the reuse loop — it would kill the turn."""
        self.assertIsNone(_reuse_latest_verdict(_GC([]), 1))
        self.assertIsNone(_reuse_latest_verdict(_GC([{'content': None}]), 1))
        self.assertIsNone(_reuse_latest_verdict(object(), 1))


if __name__ == '__main__':
    unittest.main()
