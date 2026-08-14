"""
The distributed ledger has to be keyed on the id results actually arrive under.

`_dispatch_shards` mints a local `uuid4` for each shard, then hands the shard
to `HiveTaskProtocol`, which mints its OWN id and hands it back
(`task_id = task.task_id`). The assignment used to be recorded before that
line, so the ledger entry carried the local uuid while `on_shard_result` and
`_collect_results` both report under the dispatcher's id.

`record_result` looks the entry up by task_id, finds nothing, and falls out of
its loop without raising — so every shard result was silently discarded. The
observable shape of that on the live Nunba instance: 567 ledger entries, none
with a result, across 395 runs. The distributed part of the distributed
benchmark left no evidence it had ever run.

These pin the invariant rather than the line order, so a future refactor is
free to move code as long as the ledger and the result agree on the key.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

_TMP = tempfile.mkdtemp(prefix='ledger-keying-')

import integrations.agent_engine.hive_benchmark_prover as hbp  # noqa: E402


def _fresh_prover():
    hbp._LEDGER_FILE = os.path.join(_TMP, 'ledger.json')
    hbp._LEADERBOARD_FILE = os.path.join(_TMP, 'leader.json')
    for f in (hbp._LEDGER_FILE, hbp._LEADERBOARD_FILE):
        if os.path.exists(f):
            os.remove(f)
    prover = hbp.HiveBenchmarkProver()
    prover._ledger = hbp._BenchmarkLedger()
    prover._leaderboard = hbp._Leaderboard()
    return prover


_SHARD = {
    'shard_index': 0,
    'total_shards': 1,
    'problem_count': 2,
    'problems': [{'question': 'q1', 'answer': '1'},
                 {'question': 'q2', 'answer': '2'}],
}


class _Dispatcher:
    """Stands in for HiveTaskProtocol, which reassigns the task id."""

    def __init__(self, assigned_id='dispatcher-side-id'):
        self.assigned_id = assigned_id

    def create_task(self, **kwargs):
        return MagicMock(task_id=self.assigned_id)


class TestLedgerIsKeyedOnTheDispatcherId(unittest.TestCase):

    def setUp(self):
        self.prover = _fresh_prover()
        self.nodes = [{'node_id': 'peer-A', 'type': 'peer_link'}]

    def _dispatch(self, dispatcher):
        with patch('integrations.coding_agent.hive_task_protocol.get_dispatcher',
                   return_value=dispatcher):
            return self.prover._dispatch_shards(
                run_id='run-1', benchmark_name='gsm8k',
                shards=[dict(_SHARD)], nodes=self.nodes, timeout=60)

    def test_entry_uses_the_id_the_dispatcher_returned(self):
        dispatched = self._dispatch(_Dispatcher('dispatcher-side-id'))

        entries = self.prover._ledger.get_run_entries('run-1')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['task_id'], 'dispatcher-side-id')
        # And the dispatch record the caller holds agrees with it, because
        # that is the id _collect_results will report under.
        self.assertEqual(dispatched[0]['task_id'], 'dispatcher-side-id')

    def test_a_result_under_that_id_actually_lands(self):
        """The whole point. Before the fix this silently updated nothing."""
        dispatched = self._dispatch(_Dispatcher('dispatcher-side-id'))

        self.prover._ledger.record_result(
            dispatched[0]['task_id'], 'completed', {'score': 0.5})

        entry = self.prover._ledger.get_run_entries('run-1')[0]
        self.assertEqual(entry['status'], 'completed')
        self.assertEqual(entry['result'], {'score': 0.5})
        self.assertIsNotNone(entry['completed_at'])

    def test_local_uuid_is_not_left_in_the_ledger(self):
        """A ledger keyed on the local uuid is the bug: nothing ever reports
        under it, so the entry stays assigned/None forever."""
        dispatched = self._dispatch(_Dispatcher('dispatcher-side-id'))
        entry = self.prover._ledger.get_run_entries('run-1')[0]

        self.assertNotIn('-', entry['task_id'].replace('dispatcher-side-id', ''),
                         'ledger still carries a uuid4-shaped local id')
        self.assertEqual(entry['task_id'], dispatched[0]['task_id'])

    def test_dispatch_failure_still_records_a_findable_key(self):
        """When create_task raises, the local uuid IS the id results arrive
        under, so the entry must carry it — not be skipped."""
        class _Broken:
            def create_task(self, **kwargs):
                raise RuntimeError('dispatcher down')

        dispatched = self._dispatch(_Broken())

        entries = self.prover._ledger.get_run_entries('run-1')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['task_id'], dispatched[0]['task_id'])

        self.prover._ledger.record_result(
            dispatched[0]['task_id'], 'completed', {'score': 1.0})
        self.assertEqual(
            self.prover._ledger.get_run_entries('run-1')[0]['result'],
            {'score': 1.0})

    def test_every_shard_gets_its_own_entry(self):
        """Round-robin across nodes must not collapse two shards onto one key."""
        ids = iter(['id-a', 'id-b'])

        class _Seq:
            def create_task(self, **kwargs):
                return MagicMock(task_id=next(ids))

        shards = [dict(_SHARD, shard_index=0, total_shards=2),
                  dict(_SHARD, shard_index=1, total_shards=2)]
        with patch('integrations.coding_agent.hive_task_protocol.get_dispatcher',
                   return_value=_Seq()):
            dispatched = self.prover._dispatch_shards(
                run_id='run-2', benchmark_name='gsm8k',
                shards=shards, nodes=self.nodes, timeout=60)

        keys = [e['task_id'] for e in self.prover._ledger.get_run_entries('run-2')]
        self.assertEqual(sorted(keys), ['id-a', 'id-b'])
        self.assertEqual(sorted(d['task_id'] for d in dispatched),
                         ['id-a', 'id-b'])

    def test_ledger_survives_a_reload(self):
        """It is the persisted record that has to be right — the history API
        reads it back from disk on a fresh process."""
        dispatched = self._dispatch(_Dispatcher('dispatcher-side-id'))
        self.prover._ledger.record_result(
            dispatched[0]['task_id'], 'completed', {'score': 0.5})

        reloaded = hbp._BenchmarkLedger()
        entry = [e for e in reloaded.get_run_entries('run-1')][0]
        self.assertEqual(entry['task_id'], 'dispatcher-side-id')
        self.assertEqual(entry['result'], {'score': 0.5})


if __name__ == '__main__':
    unittest.main()
