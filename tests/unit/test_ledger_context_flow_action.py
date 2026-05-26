"""#220 — verify ledger pre_assign_actions writes flow + action_id
into Task.context (the canonical slot we already had).

The agent_ledger Task model exposes a free-form `context: Dict[str, Any]`
field (core.py:267).  No new schema migration needed — flow_id /
action_id ride in there.  Frontend TaskLedgerPage parseFlowAction()
prefers context over description-regex (Nunba commit Demopage / TaskLedger
update).

This test pins the contract:
  * task.context['action_id'] is set from the input action dict
  * task.context['flow'] is set from the input action dict
  * task.context['persona'] is set from the input action dict
  * the existing add_action / pre_assign call path stamps these keys
"""
import os
import tempfile
import unittest


class LedgerContextTests(unittest.TestCase):
    def setUp(self):
        # Use a temp dir for the JSON backend so the test is hermetic.
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_ledger_with_actions(self, actions):
        from agent_ledger.core import create_ledger_from_actions
        from agent_ledger.backends import JSONBackend
        backend = JSONBackend(storage_dir=self.tmpdir)
        return create_ledger_from_actions(
            agent_id='test_agent',
            session_id='test_session',
            actions=actions,
            backend=backend,
        )

    def test_action_id_lands_in_context(self):
        ledger = self._build_ledger_with_actions([
            {'action_id': 1, 'description': 'Step 1', 'flow': 1, 'persona': 'analyst'},
            {'action_id': 2, 'description': 'Step 2', 'flow': 1, 'persona': 'analyst'},
        ])
        tasks = list(ledger.tasks.values())
        self.assertEqual(len(tasks), 2)
        # context must carry action_id + flow + persona
        for t in tasks:
            self.assertIn('action_id', t.context)
            self.assertIn('flow', t.context)
            self.assertIn('persona', t.context)
        # Specific values
        by_id = {t.context['action_id']: t for t in tasks}
        self.assertEqual(by_id[1].context['flow'], 1)
        self.assertEqual(by_id[2].context['persona'], 'analyst')

    def test_multiple_flows_distinct_in_context(self):
        """Recipes with multiple personas / flows must keep flow_id
        distinct per task — the frontend groups by it."""
        ledger = self._build_ledger_with_actions([
            {'action_id': 1, 'description': 'A', 'flow': 1, 'persona': 'analyst'},
            {'action_id': 2, 'description': 'B', 'flow': 1, 'persona': 'analyst'},
            {'action_id': 3, 'description': 'C', 'flow': 2, 'persona': 'critic'},
            {'action_id': 4, 'description': 'D', 'flow': 2, 'persona': 'critic'},
        ])
        flows = {t.context['flow'] for t in ledger.tasks.values()}
        self.assertEqual(flows, {1, 2})

    def test_task_dict_serialization_preserves_context(self):
        """to_dict() output (sent to the admin UI) must include the
        context keys — that's what TaskLedgerPage.parseFlowAction reads."""
        ledger = self._build_ledger_with_actions([
            {'action_id': 5, 'description': 'X', 'flow': 3, 'persona': 'reviewer'},
        ])
        task = next(iter(ledger.tasks.values()))
        d = task.to_dict()
        self.assertIn('context', d)
        self.assertEqual(d['context']['action_id'], 5)
        self.assertEqual(d['context']['flow'], 3)


if __name__ == '__main__':
    unittest.main()
