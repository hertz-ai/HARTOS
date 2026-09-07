"""Guard: a run that ran off the end of its recipe must not be resumed.

MEASURED LIVE 2026-09-07, agent 33323830039 — a 1-action recipe
(`execute_windows_or_android_command: open file ...tts_chatterbox...`), driven
twice in ONE process with no restart between:

    10:05:42  Retrieved current_action_id: 1   <- fresh, after the 10:05:21 restart
    10:26:54  Retrieved current_action_id: 2   <- SAME process, inherited

`current_action = 2` on a 1-action recipe is already past the end, so the second
drive terminated a phantom "Action 2" in 35 ms:

    10:27:43,452  Action 2: in_progress -> status_verification_requested
    10:27:43,462  Action 2: completed -> terminated
    10:27:43,465  [REUSE] Action 2 TERMINATED, advancing
    10:27:43,466  [REUSE] All 1 actions completed

`[FAB-GUARD]` logged 0 lines, the action's real tool never executed (its 6
mentions are `"name": ...` schema text in request bodies), and the user got a
self-introduction instead of the file status.

WHY IT PERSISTS: `user_tasks` is a process-global TTLCache
(reuse_recipe.py:4078, ttl 7200s) and the ONLY reset — `clear_action_states` +
`Action(role_actions)` at :1081-1082 — sits inside `create_agents_for_user`,
which `chat_agent` only calls under `if user_prompt not in user_agents`
(:4486). `user_agents` is itself a 2h TTLCache whose `__getitem__` does
TOUCH-ON-READ (core/session_cache.py:84), so an agent used at least once every
2h NEVER expires, never takes the cache-miss branch, and never resets. The more
an agent is used, the less likely its run state ever resets.

WHY THIS PREDICATE IS SAFE: `current_action > len(actions)` is NEVER a valid
continuation state. A genuine mid-recipe continuation always has
`current_action <= len(actions)`. So resetting here cannot truncate a run in
flight — it can only clear a run that already finished.

WHAT IT DELIBERATELY DOES NOT FIX: a run abandoned at action 3 of 10 followed by
a genuinely NEW user request still resumes at 3. Distinguishing those needs a
real run-id from the caller; this predicate does not invent one.

    python -m pytest tests/unit/test_reuse_run_boundary.py -q
"""
import unittest

from hartos.helper import Action
from hartos.reuse_recipe import _reset_pointer_if_run_finished, user_tasks


def _seed(user_prompt, n_actions, current_action):
    """Put a REAL Action in the REAL user_tasks cache, as a live run would."""
    acts = [{'action_id': i, 'action': f'step {i}'} for i in range(1, n_actions + 1)]
    a = Action(acts)
    a.current_action = current_action
    user_tasks[user_prompt] = a
    return a


class RunBoundary(unittest.TestCase):

    def tearDown(self):
        for k in ('u_finished', 'u_midrun', 'u_fresh', 'u_empty', 'u_deep'):
            try:
                del user_tasks[k]
            except Exception:
                pass

    def test_pointer_past_the_end_is_reset(self):
        """THE REGRESSION — the exact live state of 33323830039's 2nd drive."""
        a = _seed('u_finished', n_actions=1, current_action=2)
        self.assertGreater(a.current_action, len(a.actions),
                           'precondition: the pointer must be past the end')
        fired = _reset_pointer_if_run_finished('u_finished')
        self.assertTrue(fired, 'a finished run must be detected')
        self.assertEqual(user_tasks['u_finished'].current_action, 1)

    def test_mid_recipe_continuation_is_NOT_reset(self):
        """Action 3 of 10 is a run in flight — truncating it would be the bug."""
        _seed('u_midrun', n_actions=10, current_action=3)
        fired = _reset_pointer_if_run_finished('u_midrun')
        self.assertFalse(fired)
        self.assertEqual(user_tasks['u_midrun'].current_action, 3,
                         'a continuation must keep its place')

    def test_last_action_in_flight_is_NOT_reset(self):
        """current_action == len(actions) is the FINAL action, still running."""
        _seed('u_deep', n_actions=4, current_action=4)
        self.assertFalse(_reset_pointer_if_run_finished('u_deep'))
        self.assertEqual(user_tasks['u_deep'].current_action, 4)

    def test_fresh_run_is_a_no_op(self):
        _seed('u_fresh', n_actions=1, current_action=1)
        self.assertFalse(_reset_pointer_if_run_finished('u_fresh'))
        self.assertEqual(user_tasks['u_fresh'].current_action, 1)

    def test_unknown_session_is_safe(self):
        """First-ever turn: nothing cached. Must not raise into the hot path."""
        self.assertFalse(_reset_pointer_if_run_finished('never_seen_before'))

    def test_empty_action_list_is_safe(self):
        """len(actions) == 0 must not be read as 'past the end' and reset."""
        _seed('u_empty', n_actions=0, current_action=1)
        self.assertFalse(_reset_pointer_if_run_finished('u_empty'))

    def test_malformed_entry_never_raises(self):
        """A junk cache entry must not kill the turn."""
        user_tasks['u_junk'] = object()
        try:
            self.assertFalse(_reset_pointer_if_run_finished('u_junk'))
        finally:
            try:
                del user_tasks['u_junk']
            except Exception:
                pass


if __name__ == '__main__':
    unittest.main()
