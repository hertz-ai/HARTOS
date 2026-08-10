"""A delegate that never comes back must not wedge its parent forever.

THE BUG
───────
TaskDelegationBridge.delegate_task_with_tracking drives the parent task to
BLOCKED and creates a child assigned to the delegate. The ONLY thing that ever
unblocks that parent is complete_delegation_with_tracking. There was no timeout,
deadline, heartbeat, expire or reclaim anywhere in the bridge — verified by
search before this was written.

So a delegate that crashes, drops off the LAN, or is simply powered off left the
parent BLOCKED permanently, with nothing scheduled to notice. On a crowdsourced
mesh peers leaving mid-task is the NORMAL case, not an exceptional one, so this
is the difference between "distributed" and "distributed and trustworthy": a
user's work must not be lost because a stranger's laptop closed.

WHAT THIS PINS
──────────────
reclaim_stale_delegations() fails the child and unblocks the parent, going
through the SAME complete_delegation_with_tracking(success=False) path a real
failure takes — so reclaim can never drift from completion, and the ledger FSM
stays the single authority on legal transitions.

These are behavioural tests: a real SmartLedger, a real bridge, real state
transitions asserted through the ledger's own API.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
_LEDGER_SRC = os.path.join(REPO, 'agent-ledger-opensource')
if os.path.isdir(_LEDGER_SRC) and _LEDGER_SRC not in sys.path:
    sys.path.insert(0, _LEDGER_SRC)

from agent_ledger import SmartLedger, Task, TaskType, TaskStatus  # noqa: E402
from integrations.internal_comm.task_delegation_bridge import (  # noqa: E402
    TaskDelegationBridge,
)


def _a2a():
    """A2A context stubbed at the boundary — this file is about ledger state."""
    ctx = MagicMock()
    ctx.delegate_task.return_value = 'deleg-1'
    return ctx


class _Fixture:
    """A parent task with one delegation outstanding to a peer."""

    #: Counter so each fixture gets its own ledger_key and the JSON files this
    #: ledger writes never collide between tests.
    _n = 0

    def __init__(self):
        import tempfile
        _Fixture._n += 1
        # ledger_dir into a temp dir, NOT the repo's agent_data/: SmartLedger
        # persists every task to disk, and tests must not scribble into the real
        # agent data the running install reads.
        self.ledger = SmartLedger(
            agent_id='test-agent-%d' % _Fixture._n,
            session_id='test-session-%d' % _Fixture._n,
            ledger_dir=tempfile.mkdtemp(prefix='hart-deleg-'),
        )
        self.a2a = _a2a()
        self.bridge = TaskDelegationBridge(self.a2a, self.ledger)

        self.parent_id = 'parent-1'
        self.ledger.add_task(Task(
            task_id=self.parent_id,
            description='the user asked for something',
            task_type=TaskType.AUTONOMOUS,
        ))
        self.ledger.update_task_status(self.parent_id, TaskStatus.IN_PROGRESS)

        self.delegation_id = self.bridge.delegate_task_with_tracking(
            parent_task_id=self.parent_id,
            from_agent='local',
            task_description='a slice of it',
            required_skills=['anything'],
        )


class AStaleDelegationIsReclaimed(unittest.TestCase):

    def setUp(self):
        self.f = _Fixture()
        if not self.f.delegation_id:
            self.skipTest('delegate_task_with_tracking did not produce a '
                          'delegation — bridge precondition changed')

    def test_the_parent_is_BLOCKED_while_the_delegation_is_outstanding(self):
        """Precondition — this is the state that used to be permanent."""
        self.assertEqual(
            TaskStatus.BLOCKED,
            self.f.ledger.get_task(self.f.parent_id).status,
            "delegating did not block the parent; the rest of this file is "
            "testing the wrong thing")

    def test_a_FRESH_delegation_is_left_alone(self):
        """Reclaiming a merely-slow peer would be worse than the bug."""
        reclaimed = self.f.bridge.reclaim_stale_delegations(max_age_seconds=900)
        self.assertEqual([], reclaimed,
                         "a delegation handed out moments ago was reclaimed — a "
                         "slow peer on a potato would be killed mid-task")
        self.assertEqual(TaskStatus.BLOCKED,
                         self.f.ledger.get_task(self.f.parent_id).status)

    def test_an_EXPIRED_delegation_UNBLOCKS_the_parent(self):
        """The fix: the peer is gone, so the parent must get moving again."""
        later = datetime.now() + timedelta(seconds=3600)
        reclaimed = self.f.bridge.reclaim_stale_delegations(
            max_age_seconds=900, now=later)

        self.assertEqual(1, len(reclaimed),
                         "the expired delegation was not reclaimed")
        parent = self.f.ledger.get_task(self.f.parent_id)
        self.assertNotEqual(
            TaskStatus.BLOCKED, parent.status,
            "the parent is STILL BLOCKED after reclaim — this is exactly the "
            "permanent wedge the reclaim exists to prevent")

    def test_the_child_is_marked_FAILED_not_silently_dropped(self):
        """An abandoned task must be visible, not vanish from the ledger."""
        later = datetime.now() + timedelta(seconds=3600)
        self.f.bridge.reclaim_stale_delegations(max_age_seconds=900, now=later)

        mapping = self.f.bridge.delegation_map[self.f.delegation_id]
        child = self.f.ledger.get_task(mapping['child_task_id'])
        self.assertTrue(
            TaskStatus.is_terminal_state(child.status),
            "the child task was left non-terminal, so list_active_delegations "
            "will keep reporting this dead delegation as active forever")

    def test_the_reclaim_REPORTS_who_vanished(self):
        """An operator has to be able to see which peer is unreliable."""
        later = datetime.now() + timedelta(seconds=3600)
        [rec] = self.f.bridge.reclaim_stale_delegations(
            max_age_seconds=900, now=later)
        self.assertEqual(self.f.parent_id, rec['parent_task_id'])
        self.assertIn('delegated_to', rec)
        self.assertGreater(rec['age_seconds'], 900)

    def test_reclaim_is_IDEMPOTENT(self):
        """The reaper runs on a tick; a second pass must not double-fire."""
        later = datetime.now() + timedelta(seconds=3600)
        first = self.f.bridge.reclaim_stale_delegations(
            max_age_seconds=900, now=later)
        second = self.f.bridge.reclaim_stale_delegations(
            max_age_seconds=900, now=later)
        self.assertEqual(1, len(first))
        self.assertEqual(
            [], second,
            "the same delegation was reclaimed twice — the child was not left "
            "terminal, so every tick would re-fail an already-dead delegation")

    def test_a_COMPLETED_delegation_is_never_reclaimed(self):
        """Zero regression for the happy path."""
        self.f.bridge.complete_delegation_with_tracking(
            self.f.delegation_id, {'ok': True}, success=True)
        later = datetime.now() + timedelta(seconds=3600)
        self.assertEqual(
            [], self.f.bridge.reclaim_stale_delegations(
                max_age_seconds=900, now=later),
            "a delegation that already completed was reclaimed, which would "
            "fail a child that had genuinely succeeded")


class ItRefusesToGuessWhenItCannotAge(unittest.TestCase):
    """An unknown age must not be treated as brand-new OR as ancient."""

    def test_an_untimestamped_delegation_is_skipped_not_killed(self):
        f = _Fixture()
        if not f.delegation_id:
            self.skipTest('bridge precondition changed')

        # Strip every timestamp the ager can consult.
        mapping = f.bridge.delegation_map[f.delegation_id]
        mapping.pop('delegated_at', None)
        mapping.pop('created_at', None)
        child = f.ledger.get_task(mapping['child_task_id'])
        for attr in ('started_at', 'created_at'):
            if hasattr(child, attr):
                setattr(child, attr, None)

        later = datetime.now() + timedelta(days=30)
        self.assertEqual(
            [], f.bridge.reclaim_stale_delegations(max_age_seconds=900,
                                                   now=later),
            "a delegation with no usable timestamp was reclaimed on a guess — "
            "an unknown age must never be assumed ancient")


if __name__ == '__main__':
    unittest.main()
