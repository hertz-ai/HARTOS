"""A decomposed action may not finish while its own subtasks are unrun.

THE LIVE FAILURE THIS ENCODES (2026-09-10, agent 88719487304, REUSE walk
rid walk-88719487304-201935):

    20:23:12  [BREAKDOWN] action 4 persisted 2 subtask(s) (ok=True)
    20:23:12  [BREAKDOWN] action 4 has 2 pending subtask(s) - working 'Invoke the p...'
    20:23:21  reuse-w1-completed: terminal 'completed' verdict for action 4 - advancing
    20:23:21  [TARGET] Action 4: in_progress -> ... -> terminated
    20:23:21  [REUSE] Action 4 TERMINATED, advancing

Nine seconds.  execute_coding_task -- the action's whole job -- never ran, and
action 4 is the ONLY one of the nine with no FAB-GUARD verdict line, because the
fabrication gate lives on the normal advance path and this one bypasses it.

TWO HALVES, and the order matters:

  a) reuse_recipe.py:5044 advances on a terminal 'completed' verdict without
     asking whether the action still has pending subtasks.
  b) NOTHING can complete a subtask: add_subtasks() gives children the LLM's own
     subtask_id ("4.1"), while the only completion call site in reuse
     (complete_action_and_route, :6288) always builds "action_{id}" -- a PARENT
     id.  check_and_unblock_parent has ZERO call sites in hartos/ or core/.
     So get_pending_subtasks(4) returns the same children forever.

Fixing (a) alone would turn tonight's silent false-success into a permanent
wedge -- exactly the 2026-09-06 failure core/constants.py:1120-1138 records
(35 requires_breakdown verdicts, 107 rounds, 0 advances).  These tests pin the
whole contract that file already writes down:

    add_subtasks() -> get_pending_subtasks() -> execute each
        -> check_and_unblock_parent() -> parent completes

The 2026-09-06 fix wired hops 1-3.  Hop 4 was never wired.
"""

import io
import shutil
import tempfile
import unittest

from agent_ledger.core import SmartLedger, Task, TaskType, TaskStatus


def _ledger(tmp):
    return SmartLedger(agent_id='t', session_id='s', ledger_dir=tmp)


def _seeded(tmp, n_subtasks=2):
    """A ledger holding action_4 decomposed into `n_subtasks` children."""
    led = _ledger(tmp)
    led.add_task(Task(task_id='action_4', description='extract specs via code',
                      task_type=TaskType.AUTONOMOUS))
    led.add_subtasks(4, [{'subtask_id': '4.%d' % i,
                          'description': 'child %d' % i}
                         for i in range(1, n_subtasks + 1)])
    return led


class TestLedgerDecomposition(unittest.TestCase):
    """The ledger's own half -- these should already hold."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_subtasks_creates_pending_children(self):
        led = _seeded(self.tmp)
        self.assertEqual(len(led.get_pending_subtasks(4)), 2)

    def test_children_carry_the_llm_subtask_id_not_an_action_id(self):
        """Why "action_{id}" can never name a child."""
        led = _seeded(self.tmp)
        ids = sorted(t.task_id for t in led.get_pending_subtasks(4))
        self.assertEqual(ids, ['4.1', '4.2'])

    def test_a_never_started_child_cannot_be_completed(self):
        """The contract I got wrong, pinned so nobody repeats it.

        My first draft of this file asserted that complete_task_and_route on a
        PENDING child closes it.  MEASURED 2026-09-10 -- it does not, and it
        does not complain either:

            complete_task('4.1','success')           -> False, still 'pending'
            complete_task_and_route('4.1','success') -> Task,  still 'pending'

        A silent refusal is why hop 4 could never have worked, whoever called
        it.  This is the reason _reuse_complete_pending_subtask marks the child
        IN_PROGRESS first, and why the BREAKDOWN block marks the one it steers.
        """
        led = _seeded(self.tmp)
        child = led.get_pending_subtasks(4)[0]
        self.assertFalse(led.complete_task('4.1', 'success'))
        led.complete_task_and_route(child.task_id, 'success')
        self.assertEqual(led.tasks[child.task_id].status, TaskStatus.PENDING,
                         'ledger completed a child that was never started')

    def test_completing_every_started_child_clears_the_pending_set(self):
        """Hop 4 works once the transition the ledger requires is honoured."""
        led = _seeded(self.tmp)
        for t in list(led.get_pending_subtasks(4)):
            t.status = TaskStatus.IN_PROGRESS
            led.complete_task_and_route(t.task_id, 'success')
        self.assertEqual(led.get_pending_subtasks(4), [],
                         'children still pending after completing each')
        self.assertNotEqual(led.tasks['action_4'].status, TaskStatus.BLOCKED,
                            'parent still BLOCKED after every child completed')


class TestReuseCompletesSubtasksBeforeTheParent(unittest.TestCase):
    """The reuse half -- RED until hop 4 is wired."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reuse(self):
        import hartos.reuse_recipe as rr
        return rr

    def test_reuse_exposes_a_way_to_complete_a_subtask(self):
        """A 'completed' verdict while children pend must close a CHILD.

        complete_action_and_route builds "action_{id}" and takes an int, so the
        pipeline has no way to say "4.1 is done".  The contract needs one
        callable that closes the next pending child of an action.
        """
        rr = self._reuse()
        self.assertTrue(
            hasattr(rr, '_reuse_complete_pending_subtask'),
            'reuse has no way to complete a subtask, so a decomposed action '
            'can never finish honestly (core/constants.py:1124-1128 hop 4)')

    def test_completing_a_pending_subtask_shrinks_the_pending_set(self):
        rr = self._reuse()
        fn = getattr(rr, '_reuse_complete_pending_subtask', None)
        if fn is None:
            self.fail('_reuse_complete_pending_subtask missing')
        led = _seeded(self.tmp)
        ledgers = {'u_1': led}
        self.assertTrue(fn('u_1', 4, ledgers),
                        'reported no subtask completed while 2 were pending')
        self.assertEqual(len(led.get_pending_subtasks(4)), 1)

    def test_it_reports_false_when_there_is_nothing_pending(self):
        """So the caller can tell "worked a child" from "parent may advance"."""
        rr = self._reuse()
        fn = getattr(rr, '_reuse_complete_pending_subtask', None)
        if fn is None:
            self.fail('_reuse_complete_pending_subtask missing')
        led = _ledger(self.tmp)
        led.add_task(Task(task_id='action_7', description='no children',
                          task_type=TaskType.AUTONOMOUS))
        self.assertFalse(fn('u_1', 7, {'u_1': led}))


class TestAdvanceChokepointConsultsSubtasks(unittest.TestCase):
    """_advance_or_steer is the ONE door all six advance sites go through."""

    def _src(self):
        import hartos.reuse_recipe as rr
        return io.open(rr.__file__, encoding='utf-8', errors='replace').read()

    def _advance_or_steer_body(self):
        src = self._src()
        start = src.index('def _advance_or_steer(')
        nxt = src.index(chr(10) + 'def ', start + 1)
        return src[start:nxt]

    def test_the_chokepoint_asks_about_pending_subtasks(self):
        body = self._advance_or_steer_body()
        self.assertIn(
            'get_pending_subtasks', body,
            '_advance_or_steer advances without ever asking whether the action '
            'still has unrun subtasks -- the force-completion measured on '
            'action 4 of agent 88719487304 at 20:23:21')

    def test_every_advance_site_still_funnels_through_the_one_door(self):
        """A 7th advance site added elsewhere would dodge the guard."""
        src = self._src()
        self.assertGreaterEqual(
            src.count('_advance_or_steer('), 6,
            'expected the 6 known call sites plus the def; re-point this guard')
        self.assertEqual(
            src.count('next_action_id, advanced = _advance_reuse_action('), 1,
            'more than one place now moves the action pointer -- the guard at '
            'the chokepoint no longer covers every advance')


if __name__ == '__main__':
    unittest.main()
