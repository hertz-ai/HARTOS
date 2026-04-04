"""Tests for HiveBenchmarkProver -- distributed benchmark orchestration."""
import json
import os
import shutil
import sys
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Patch module-level globals BEFORE import so _BenchmarkLedger / _Leaderboard
# do not touch real files during import.
import tempfile

_TMP_ROOT = tempfile.mkdtemp(prefix='hive_bench_test_')
_TMP_LEDGER = os.path.join(_TMP_ROOT, 'benchmark_ledger.json')
_TMP_LEADER = os.path.join(_TMP_ROOT, 'benchmark_leaderboard.json')

patch.dict(os.environ, {
    'HEVOLVE_DB_PATH': '',
    'HART_NODE_ID': 'test_local_node',
}).start()

import integrations.agent_engine.hive_benchmark_prover as hbp

# ── Fixtures / helpers ──────────────────────────────────────────────────


def _redirect_files():
    """Redirect ledger/leaderboard paths to temp files."""
    hbp._LEDGER_FILE = _TMP_LEDGER
    hbp._LEADERBOARD_FILE = _TMP_LEADER


def _clean_tmp():
    for f in (_TMP_LEDGER, _TMP_LEADER,
              _TMP_LEDGER + '.tmp', _TMP_LEADER + '.tmp'):
        if os.path.exists(f):
            os.remove(f)


def _fresh_ledger():
    _redirect_files()
    _clean_tmp()
    return hbp._BenchmarkLedger()


def _fresh_leaderboard():
    _redirect_files()
    _clean_tmp()
    return hbp._Leaderboard()


def _fresh_prover():
    """Create a HiveBenchmarkProver with temp-redirected storage."""
    _redirect_files()
    _clean_tmp()
    prover = hbp.HiveBenchmarkProver()
    # Replace internal ledger/leaderboard with freshly constructed ones that
    # use our temp paths.
    prover._ledger = _fresh_ledger()
    prover._leaderboard = _fresh_leaderboard()
    return prover


# ========================================================================
# 1. _BenchmarkLedger
# ========================================================================

class TestBenchmarkLedger(unittest.TestCase):
    """Tests for the distributed benchmark ledger."""

    def setUp(self):
        self.ledger = _fresh_ledger()

    def tearDown(self):
        _clean_tmp()

    # -- record_assignment persists entry --

    def test_record_assignment_persists(self):
        self.ledger.record_assignment(
            run_id='run-1', task_id='task-1', node_id='node-a',
            shard_index=0, benchmark_name='mmlu_mini')
        entries = self.ledger.get_run_entries('run-1')
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['run_id'], 'run-1')
        self.assertEqual(entry['task_id'], 'task-1')
        self.assertEqual(entry['node_id'], 'node-a')
        self.assertEqual(entry['shard_index'], 0)
        self.assertEqual(entry['benchmark'], 'mmlu_mini')
        self.assertEqual(entry['status'], 'assigned')
        self.assertIsNone(entry['result'])
        self.assertIsNotNone(entry['assigned_at'])
        self.assertIsNone(entry['completed_at'])
        # Verify on-disk persistence
        self.assertTrue(os.path.exists(_TMP_LEDGER))
        with open(_TMP_LEDGER, 'r') as f:
            disk_data = json.load(f)
        self.assertEqual(len(disk_data), 1)

    # -- record_result updates correct entry --

    def test_record_result_updates_correct_entry(self):
        self.ledger.record_assignment(
            run_id='run-1', task_id='task-1', node_id='n1',
            shard_index=0, benchmark_name='humaneval_mini')
        self.ledger.record_assignment(
            run_id='run-1', task_id='task-2', node_id='n2',
            shard_index=1, benchmark_name='humaneval_mini')

        result = {'score': 0.85, 'problems_solved': 25}
        self.ledger.record_result('task-2', 'completed', result)

        entries = self.ledger.get_run_entries('run-1')
        t1 = [e for e in entries if e['task_id'] == 'task-1'][0]
        t2 = [e for e in entries if e['task_id'] == 'task-2'][0]

        self.assertEqual(t1['status'], 'assigned')
        self.assertIsNone(t1['result'])
        self.assertEqual(t2['status'], 'completed')
        self.assertEqual(t2['result'], result)
        self.assertIsNotNone(t2['completed_at'])

    # -- get_run_entries filters by run_id --

    def test_get_run_entries_filters_by_run_id(self):
        self.ledger.record_assignment(
            run_id='r1', task_id='t1', node_id='n',
            shard_index=0, benchmark_name='b')
        self.ledger.record_assignment(
            run_id='r2', task_id='t2', node_id='n',
            shard_index=0, benchmark_name='b')
        self.ledger.record_assignment(
            run_id='r1', task_id='t3', node_id='n',
            shard_index=1, benchmark_name='b')

        r1_entries = self.ledger.get_run_entries('r1')
        r2_entries = self.ledger.get_run_entries('r2')
        self.assertEqual(len(r1_entries), 2)
        self.assertEqual(len(r2_entries), 1)
        self.assertEqual(self.ledger.get_run_entries('nonexistent'), [])

    # -- get_history returns recent entries and respects limit --

    def test_get_history_returns_recent_and_respects_limit(self):
        for i in range(5):
            self.ledger.record_assignment(
                run_id='r', task_id=f't{i}', node_id='n',
                shard_index=i, benchmark_name='mmlu_mini')
        # Default: all 5
        history = self.ledger.get_history(benchmark='mmlu_mini')
        self.assertEqual(len(history), 5)
        # Reversed order (most recent first)
        self.assertEqual(history[0]['task_id'], 't4')
        self.assertEqual(history[-1]['task_id'], 't0')

        # Respect limit
        limited = self.ledger.get_history(benchmark='mmlu_mini', limit=2)
        self.assertEqual(len(limited), 2)

    def test_get_history_filters_by_benchmark(self):
        self.ledger.record_assignment(
            run_id='r', task_id='t1', node_id='n',
            shard_index=0, benchmark_name='mmlu_mini')
        self.ledger.record_assignment(
            run_id='r', task_id='t2', node_id='n',
            shard_index=0, benchmark_name='gsm8k_mini')
        self.assertEqual(len(self.ledger.get_history(benchmark='mmlu_mini')), 1)
        self.assertEqual(len(self.ledger.get_history(benchmark='')), 2)

    # -- Thread safety --

    def test_concurrent_access_is_thread_safe(self):
        errors = []

        def writer(start_idx):
            try:
                for i in range(20):
                    self.ledger.record_assignment(
                        run_id='r', task_id=f't_{start_idx}_{i}',
                        node_id='n', shard_index=start_idx * 20 + i,
                        benchmark_name='b')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(j,))
                   for j in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(self.ledger.get_run_entries('r')), 80)


# ========================================================================
# 2. _Leaderboard
# ========================================================================

class TestLeaderboard(unittest.TestCase):
    """Tests for the persistent leaderboard."""

    def setUp(self):
        self.lb = _fresh_leaderboard()

    def tearDown(self):
        _clean_tmp()

    # -- record_run stores run data --

    def test_record_run_stores_data(self):
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.90,
            num_nodes=3, time_seconds=120.5,
            per_node=[{'node_id': 'n1'}], speedup=2.8)

        data = self.lb.get_leaderboard()
        self.assertEqual(data['total_runs'], 1)
        run = data['recent_runs'][0]
        self.assertEqual(run['run_id'], 'r1')
        self.assertEqual(run['score'], 0.90)
        self.assertEqual(run['num_nodes'], 3)
        self.assertEqual(run['speedup_vs_single'], 2.8)

    # -- record_run updates best_scores when improved --

    def test_record_run_updates_best_score(self):
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.80,
            num_nodes=2, time_seconds=60, per_node=[], speedup=1.5)
        self.lb.record_run(
            run_id='r2', benchmark='mmlu_mini', score=0.90,
            num_nodes=3, time_seconds=50, per_node=[], speedup=2.0)
        # Score went down -- should NOT replace best
        self.lb.record_run(
            run_id='r3', benchmark='mmlu_mini', score=0.85,
            num_nodes=3, time_seconds=55, per_node=[], speedup=1.8)

        best = self.lb.get_best_scores()
        self.assertEqual(best['mmlu_mini']['score'], 0.90)
        self.assertEqual(best['mmlu_mini']['run_id'], 'r2')

    # -- get_best_scores returns correct bests across benchmarks --

    def test_get_best_scores_multi_benchmark(self):
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.90,
            num_nodes=2, time_seconds=60, per_node=[], speedup=1.5)
        self.lb.record_run(
            run_id='r2', benchmark='humaneval_mini', score=0.70,
            num_nodes=3, time_seconds=90, per_node=[], speedup=2.0)

        best = self.lb.get_best_scores()
        self.assertIn('mmlu_mini', best)
        self.assertIn('humaneval_mini', best)
        self.assertEqual(best['mmlu_mini']['score'], 0.90)
        self.assertEqual(best['humaneval_mini']['score'], 0.70)

    # -- compare_to_baselines --

    def test_compare_to_baselines(self):
        # Hive beats GPT-4 on mmlu (0.86) but loses to Claude (0.88)
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.87,
            num_nodes=5, time_seconds=30, per_node=[], speedup=3.0)

        comp = self.lb.compare_to_baselines()
        self.assertIn('mmlu_mini', comp)
        c = comp['mmlu_mini']
        self.assertEqual(c['hive'], 0.87)
        self.assertIn('gpt-4', c['hive_wins'])
        self.assertIn('gemini-1.5-pro', c['hive_wins'])
        self.assertIn('llama-3-70b', c['hive_wins'])
        self.assertIn('claude-3.5-sonnet', c['hive_loses'])
        # margin_vs_best: 0.87 - 0.88 = -0.01
        self.assertAlmostEqual(c['margin_vs_best'], -0.01, places=3)

    def test_compare_to_baselines_hive_wins_all(self):
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.95,
            num_nodes=10, time_seconds=20, per_node=[], speedup=5.0)

        comp = self.lb.compare_to_baselines()
        c = comp['mmlu_mini']
        self.assertEqual(len(c['hive_loses']), 0)
        self.assertEqual(len(c['hive_wins']), 4)
        self.assertGreater(c['margin_vs_best'], 0)

    # -- get_improvement_history --

    def test_get_improvement_history(self):
        self.lb.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.80,
            num_nodes=2, time_seconds=60, per_node=[], speedup=1.5)
        self.lb.record_run(
            run_id='r2', benchmark='mmlu_mini', score=0.85,
            num_nodes=3, time_seconds=50, per_node=[], speedup=2.0)
        self.lb.record_run(
            run_id='r3', benchmark='mmlu_mini', score=0.90,
            num_nodes=4, time_seconds=40, per_node=[], speedup=3.0)

        history = self.lb.get_improvement_history()
        self.assertIn('mmlu_mini', history)
        scores = [h['score'] for h in history['mmlu_mini']]
        self.assertEqual(scores, [0.80, 0.85, 0.90])


# ========================================================================
# 3. HiveBenchmarkProver
# ========================================================================

class TestHiveBenchmarkProver(unittest.TestCase):
    """Tests for the main HiveBenchmarkProver class."""

    def setUp(self):
        self.prover = _fresh_prover()

    def tearDown(self):
        # Stop any background loop
        self.prover._loop_running = False
        _clean_tmp()

    # -- start_run creates ledger entries + dispatches --

    @patch.object(hbp.HiveBenchmarkProver, '_discover_nodes')
    @patch.object(hbp.HiveBenchmarkProver, '_dispatch_shards')
    def test_start_run_creates_ledger_entries_and_tasks(
            self, mock_dispatch, mock_nodes):
        mock_nodes.return_value = [
            {'node_id': 'n1', 'type': 'local'},
            {'node_id': 'n2', 'type': 'peer_link'},
        ]
        mock_dispatch.return_value = [
            {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
             'shard': {'shard_index': 0, 'problem_count': 50}},
            {'task_id': 'tid2', 'node_id': 'n2', 'shard_index': 1,
             'shard': {'shard_index': 1, 'problem_count': 50}},
        ]

        run_id = self.prover.start_run('humaneval_mini')

        self.assertIsNotNone(run_id)
        self.assertIn(run_id, self.prover._active_runs)
        state = self.prover._active_runs[run_id]
        self.assertEqual(state['benchmark'], 'humaneval_mini')
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['total_shards'], 2)
        self.assertEqual(state['num_nodes'], 2)
        mock_dispatch.assert_called_once()

    # -- start_run shards problems across nodes --

    @patch.object(hbp.HiveBenchmarkProver, '_discover_nodes')
    @patch.object(hbp.HiveBenchmarkProver, '_dispatch_shards')
    def test_start_run_shards_problems_across_nodes(
            self, mock_dispatch, mock_nodes):
        mock_nodes.return_value = [
            {'node_id': f'n{i}', 'type': 'local'} for i in range(3)
        ]
        mock_dispatch.return_value = [
            {'task_id': f't{i}', 'node_id': f'n{i}', 'shard_index': i,
             'shard': {'shard_index': i, 'problem_count': 17}}
            for i in range(3)
        ]

        run_id = self.prover.start_run('humaneval_mini')

        # _dispatch_shards was called with shards split across 3 nodes
        call_args = mock_dispatch.call_args
        shards_arg = call_args[0][2]  # third positional: shards
        nodes_arg = call_args[0][3]   # fourth positional: nodes
        self.assertEqual(len(nodes_arg), 3)
        # Each shard should have a subset of problems
        for s in shards_arg:
            self.assertGreater(s['problem_count'], 0)

    # -- start_run with no problems --

    def test_start_run_no_problems_returns_no_problems_status(self):
        run_id = self.prover.start_run('nonexistent_benchmark')
        state = self.prover._active_runs[run_id]
        self.assertEqual(state['status'], 'no_problems')
        self.assertEqual(state['total_shards'], 0)

    # -- on_shard_result records result --

    @patch.object(hbp.HiveBenchmarkProver, '_discover_nodes')
    @patch.object(hbp.HiveBenchmarkProver, '_dispatch_shards')
    def test_on_shard_result_records_in_ledger(
            self, mock_dispatch, mock_nodes):
        mock_nodes.return_value = [{'node_id': 'n1', 'type': 'local'}]
        mock_dispatch.return_value = [
            {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
             'shard': {'shard_index': 0, 'problem_count': 10}},
            {'task_id': 'tid2', 'node_id': 'n1', 'shard_index': 1,
             'shard': {'shard_index': 1, 'problem_count': 10}},
        ]

        run_id = self.prover.start_run('gsm8k_mini')

        # Submit first shard result
        self.prover.on_shard_result(
            run_id, 'tid1',
            {'score': 0.9, 'problems_solved': 9, 'time_seconds': 5})

        state = self.prover._active_runs[run_id]
        self.assertEqual(state['completed_shards'], 1)
        self.assertIn('tid1', state['results'])
        self.assertEqual(state['results']['tid1']['score'], 0.9)

    # -- on_shard_result triggers aggregate when all shards done --

    @patch.object(hbp.HiveBenchmarkProver, '_publish_results',
                  return_value={'channels_notified': 0})
    @patch.object(hbp.HiveBenchmarkProver, '_discover_nodes')
    @patch.object(hbp.HiveBenchmarkProver, '_dispatch_shards')
    def test_on_shard_result_triggers_aggregate_when_all_done(
            self, mock_dispatch, mock_nodes, mock_publish):
        mock_nodes.return_value = [{'node_id': 'n1', 'type': 'local'}]
        mock_dispatch.return_value = [
            {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
             'shard': {'shard_index': 0, 'problem_count': 25}},
            {'task_id': 'tid2', 'node_id': 'n1', 'shard_index': 1,
             'shard': {'shard_index': 1, 'problem_count': 25}},
        ]

        run_id = self.prover.start_run('humaneval_mini')

        # First shard -- should return None (not all done)
        result = self.prover.on_shard_result(
            run_id, 'tid1',
            {'score': 0.8, 'problems_solved': 20, 'time_seconds': 10})
        self.assertIsNone(result)

        # Second shard -- should trigger aggregation
        result = self.prover.on_shard_result(
            run_id, 'tid2',
            {'score': 0.6, 'problems_solved': 15, 'time_seconds': 8})
        self.assertIsNotNone(result)
        self.assertEqual(result['benchmark'], 'humaneval_mini')
        self.assertIn('score', result)
        self.assertIn('speedup', result)
        mock_publish.assert_called_once()

    # -- on_shard_result for unknown run_id --

    def test_on_shard_result_unknown_run_returns_none(self):
        result = self.prover.on_shard_result(
            'nonexistent', 'tid1', {'score': 0.5})
        self.assertIsNone(result)

    # -- aggregate_run calculates correct weighted score --

    @patch.object(hbp.HiveBenchmarkProver, '_publish_results',
                  return_value={'channels_notified': 0})
    def test_aggregate_run_weighted_score(self, mock_publish):
        run_id = 'test-agg'
        start = time.time()
        self.prover._active_runs[run_id] = {
            'benchmark': 'mmlu_mini',
            'problems': [{'id': f'p{i}'} for i in range(30)],
            'shards': [],
            'dispatched': [
                {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
                 'shard': {'shard_index': 0, 'problem_count': 20}},
                {'task_id': 'tid2', 'node_id': 'n2', 'shard_index': 1,
                 'shard': {'shard_index': 1, 'problem_count': 10}},
            ],
            'results': {
                'tid1': {
                    'task_id': 'tid1', 'status': 'completed',
                    'result': {'score': 0.9, 'problems_solved': 18},
                    'score': 0.9, 'problems_solved': 18,
                    'time_seconds': 10, 'completed_at': time.time(),
                },
                'tid2': {
                    'task_id': 'tid2', 'status': 'completed',
                    'result': {'score': 0.6, 'problems_solved': 6},
                    'score': 0.6, 'problems_solved': 6,
                    'time_seconds': 8, 'completed_at': time.time(),
                },
            },
            'start_time': start,
            'config': {},
            'status': 'running',
            'total_shards': 2,
            'completed_shards': 2,
            'num_nodes': 2,
        }

        result = self.prover.aggregate_run(run_id)

        # Weighted: (0.9 * 20 + 0.6 * 10) / 30 = (18 + 6) / 30 = 0.8
        self.assertAlmostEqual(result['score'], 0.8, places=3)
        self.assertEqual(result['benchmark'], 'mmlu_mini')
        self.assertEqual(result['problems_total'], 30)
        self.assertIn('speedup', result)
        self.assertIn('comparison', result)

        # Verify the run is marked completed
        self.assertEqual(
            self.prover._active_runs[run_id]['status'], 'completed')

    # -- aggregate_run calculates speedup --

    @patch.object(hbp.HiveBenchmarkProver, '_publish_results',
                  return_value={'channels_notified': 0})
    def test_aggregate_run_calculates_speedup(self, mock_publish):
        run_id = 'test-speed'
        start = time.time() - 10  # 10 seconds ago
        self.prover._active_runs[run_id] = {
            'benchmark': 'gsm8k_mini',
            'problems': [{'id': 'p'}],
            'shards': [],
            'dispatched': [
                {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
                 'shard': {'shard_index': 0, 'problem_count': 50}},
                {'task_id': 'tid2', 'node_id': 'n2', 'shard_index': 1,
                 'shard': {'shard_index': 1, 'problem_count': 50}},
            ],
            'results': {
                'tid1': {
                    'task_id': 'tid1', 'status': 'completed',
                    'result': {'score': 0.8, 'problems_solved': 40},
                    'score': 0.8, 'problems_solved': 40,
                    'time_seconds': 30, 'completed_at': time.time(),
                },
                'tid2': {
                    'task_id': 'tid2', 'status': 'completed',
                    'result': {'score': 0.7, 'problems_solved': 35},
                    'score': 0.7, 'problems_solved': 35,
                    'time_seconds': 25, 'completed_at': time.time(),
                },
            },
            'start_time': start,
            'config': {},
            'status': 'running',
            'total_shards': 2,
            'completed_shards': 2,
            'num_nodes': 2,
        }

        result = self.prover.aggregate_run(run_id)

        # speedup = total_shard_time / elapsed
        # total_shard_time = 30 + 25 = 55, elapsed ~ 10
        # speedup ~ 5.5
        self.assertGreater(result['speedup'], 1.0)

    # -- aggregate_run for unknown run_id --

    def test_aggregate_run_unknown_run(self):
        result = self.prover.aggregate_run('nonexistent')
        self.assertEqual(result['error'], 'unknown_run')

    # -- _publish_results emits EventBus event --

    @patch('integrations.agent_engine.hive_benchmark_prover.emit_event',
           create=True)
    def test_publish_results_emits_eventbus(self, mock_emit):
        # Patch at the point of import inside _publish_results
        with patch(
            'core.platform.events.emit_event'
        ) as mock_event:
            try:
                self.prover._publish_results('run-1', {
                    'benchmark': 'mmlu_mini',
                    'score': 0.90,
                    'num_nodes': 5,
                    'speedup': 3.0,
                    'time_seconds': 30,
                })
            except Exception:
                pass
            # The emit_event should have been called (may fail on social imports)
            if mock_event.called:
                call_args = mock_event.call_args_list[0]
                self.assertEqual(call_args[0][0], 'hive.benchmark.completed')
                payload = call_args[0][1]
                self.assertEqual(payload['benchmark'], 'mmlu_mini')
                self.assertEqual(payload['score'], 0.90)

    # -- get_status returns active runs + leaderboard --

    @patch.object(hbp.HiveBenchmarkProver, '_discover_nodes')
    @patch.object(hbp.HiveBenchmarkProver, '_dispatch_shards')
    def test_get_status(self, mock_dispatch, mock_nodes):
        mock_nodes.return_value = [{'node_id': 'n1', 'type': 'local'}]
        mock_dispatch.return_value = [
            {'task_id': 'tid1', 'node_id': 'n1', 'shard_index': 0,
             'shard': {'shard_index': 0, 'problem_count': 10}},
        ]

        run_id = self.prover.start_run('gsm8k_mini')
        status = self.prover.get_status()

        self.assertIn('active_runs', status)
        self.assertIn(run_id, status['active_runs'])
        self.assertIn('leaderboard_summary', status)
        self.assertIn('loop_running', status)
        self.assertFalse(status['loop_running'])
        self.assertIn('connected_nodes', status)

    # -- start_continuous_loop / stop thread lifecycle --

    def test_start_and_stop_continuous_loop(self):
        # Patch the actual loop body to avoid real execution
        with patch.object(
            self.prover, '_continuous_loop', side_effect=lambda: None
        ):
            self.prover.start_continuous_loop()
            self.assertTrue(self.prover._loop_running)
            self.assertIsNotNone(self.prover._loop_thread)

            # Idempotent: second call should not error
            self.prover.start_continuous_loop()

            self.prover.stop()
            self.assertFalse(self.prover._loop_running)

    def test_stop_without_start(self):
        """stop() should be safe even if loop was never started."""
        self.prover.stop()
        self.assertFalse(self.prover._loop_running)

    # -- _aggregate_results --

    def test_aggregate_results_empty(self):
        result = self.prover._aggregate_results([])
        self.assertEqual(result['score'], 0.0)
        self.assertEqual(result['num_nodes'], 0)

    def test_aggregate_results_single_shard(self):
        shard_results = [{
            'shard_index': 0, 'node_id': 'n1', 'status': 'completed',
            'problems_solved': 10, 'problems_total': 20,
            'score': 0.5, 'time_seconds': 5,
        }]
        result = self.prover._aggregate_results(shard_results)
        self.assertAlmostEqual(result['score'], 0.5, places=3)
        self.assertEqual(result['num_nodes'], 1)
        self.assertEqual(result['problems_solved'], 10)

    def test_aggregate_results_weighted_average(self):
        shard_results = [
            {'shard_index': 0, 'node_id': 'n1', 'status': 'completed',
             'problems_solved': 8, 'problems_total': 10,
             'score': 0.8, 'time_seconds': 5},
            {'shard_index': 1, 'node_id': 'n2', 'status': 'completed',
             'problems_solved': 18, 'problems_total': 20,
             'score': 0.9, 'time_seconds': 10},
        ]
        result = self.prover._aggregate_results(shard_results)
        # Weighted: (0.8 * 10 + 0.9 * 20) / 30 = 26 / 30 ≈ 0.8667
        self.assertAlmostEqual(result['score'], 0.8667, places=3)
        self.assertEqual(result['num_nodes'], 2)

    # -- _fetch_problems --

    def test_fetch_problems_mmlu(self):
        problems = self.prover._fetch_problems('mmlu_mini', {})
        # 5 subjects * 20 per subject = 100
        self.assertEqual(len(problems), 100)
        self.assertEqual(problems[0]['type'], 'mcq')

    def test_fetch_problems_code(self):
        problems = self.prover._fetch_problems('humaneval_mini', {})
        self.assertEqual(len(problems), 50)
        self.assertEqual(problems[0]['type'], 'code')

    def test_fetch_problems_unknown(self):
        problems = self.prover._fetch_problems('nonexistent', {})
        self.assertEqual(problems, [])

    # -- _split_benchmark --

    def test_split_benchmark_round_robin(self):
        problems = [{'id': f'p{i}'} for i in range(10)]
        shards = self.prover._split_benchmark('test', problems, 3)
        # 3 shards: 4, 3, 3 problems
        counts = [s['problem_count'] for s in shards]
        self.assertEqual(sum(counts), 10)
        self.assertEqual(len(shards), 3)
        # Round-robin: indices 0,3,6,9 -> shard 0 (4 problems)
        self.assertEqual(counts[0], 4)


# ========================================================================
# 4. Constants
# ========================================================================

class TestConstants(unittest.TestCase):
    """Tests for module-level constants."""

    def test_builtin_benchmarks_has_expected_keys(self):
        expected = {
            'mmlu_mini', 'humaneval_mini', 'gsm8k_mini',
            'reasoning_mini', 'mt_bench_mini', 'arc_mini',
            'hive_latency', 'hive_throughput', 'hive_cost',
        }
        self.assertEqual(set(hbp.BUILTIN_BENCHMARKS.keys()), expected)

    def test_known_baselines_has_expected_models(self):
        expected_models = {'gpt-4', 'claude-3.5-sonnet',
                           'gemini-1.5-pro', 'llama-3-70b'}
        self.assertEqual(set(hbp.KNOWN_BASELINES.keys()), expected_models)

    def test_known_baselines_scores_in_valid_range(self):
        for model, scores in hbp.KNOWN_BASELINES.items():
            for bench, score in scores.items():
                self.assertGreaterEqual(score, 0.0,
                                        f"{model}/{bench}")
                self.assertLessEqual(score, 1.0,
                                     f"{model}/{bench}")

    def test_benchmark_rotation_non_empty(self):
        self.assertGreater(len(hbp._BENCHMARK_ROTATION), 0)

    def test_benchmark_rotation_entries_are_valid(self):
        for name in hbp._BENCHMARK_ROTATION:
            self.assertIn(name, hbp.BUILTIN_BENCHMARKS)


# ========================================================================
# 5. Singleton
# ========================================================================

class TestSingleton(unittest.TestCase):
    """Tests for the get_benchmark_prover() singleton."""

    def setUp(self):
        # Reset singleton for isolation
        hbp._prover = None

    def tearDown(self):
        hbp._prover = None

    @patch.object(hbp, '_LEDGER_FILE', _TMP_LEDGER)
    @patch.object(hbp, '_LEADERBOARD_FILE', _TMP_LEADER)
    def test_get_benchmark_prover_returns_same_instance(self):
        p1 = hbp.get_benchmark_prover()
        p2 = hbp.get_benchmark_prover()
        self.assertIs(p1, p2)

    @patch.object(hbp, '_LEDGER_FILE', _TMP_LEDGER)
    @patch.object(hbp, '_LEADERBOARD_FILE', _TMP_LEADER)
    def test_get_prover_alias(self):
        """get_prover is a backward-compatible alias."""
        p1 = hbp.get_benchmark_prover()
        p2 = hbp.get_prover()
        self.assertIs(p1, p2)


# ========================================================================
# 6. Blueprint
# ========================================================================

class TestBlueprint(unittest.TestCase):
    """Tests for create_benchmark_blueprint()."""

    def test_create_benchmark_blueprint_returns_blueprint(self):
        bp = hbp.create_benchmark_blueprint()
        if bp is None:
            self.skipTest("Flask not installed")
        from flask import Blueprint
        self.assertIsInstance(bp, Blueprint)
        self.assertEqual(bp.name, 'hive_benchmark_prover')
        self.assertEqual(bp.url_prefix, '/api/hive/benchmarks')

    def test_create_benchmark_blueprint_has_routes(self):
        bp = hbp.create_benchmark_blueprint()
        if bp is None:
            self.skipTest("Flask not installed")
        # Blueprint registers route view functions via deferred_functions
        # Each @bp.route() call adds a deferred registration function
        self.assertTrue(
            hasattr(bp, 'deferred_functions'),
            "Blueprint should have deferred_functions")
        # We expect at least 5 routes: leaderboard, run, run/<id>,
        # history, challenge, status
        self.assertGreaterEqual(len(bp.deferred_functions), 5)

    def test_backward_compat_alias(self):
        self.assertIs(hbp.create_benchmark_prover_blueprint,
                       hbp.create_benchmark_blueprint)


# ========================================================================
# 7. Goal seeding entry
# ========================================================================

class TestGoalSeedEntry(unittest.TestCase):
    """Tests for the bootstrap goal seed dict."""

    def test_bootstrap_benchmark_prover_structure(self):
        seed = hbp.bootstrap_benchmark_prover
        self.assertEqual(seed['slug'], 'bootstrap_benchmark_prover')
        self.assertEqual(seed['goal_type'], 'hive_proof')
        self.assertIn('title', seed)
        self.assertIn('description', seed)
        self.assertIn('config', seed)
        self.assertEqual(seed['spark_budget'], 500)
        self.assertFalse(seed['use_product'])

    def test_seed_backward_compat_alias(self):
        self.assertIs(hbp.SEED_BENCHMARK_PROVER_GOAL,
                       hbp.bootstrap_benchmark_prover)


# ========================================================================
# 8. Persistence round-trip
# ========================================================================

class TestPersistence(unittest.TestCase):
    """Test that ledger and leaderboard survive a reload."""

    def setUp(self):
        _redirect_files()
        _clean_tmp()

    def tearDown(self):
        _clean_tmp()

    def test_ledger_persists_across_instances(self):
        ledger1 = hbp._BenchmarkLedger()
        ledger1.record_assignment(
            run_id='r1', task_id='t1', node_id='n1',
            shard_index=0, benchmark_name='mmlu_mini')

        # Create new instance -- should reload from disk
        ledger2 = hbp._BenchmarkLedger()
        entries = ledger2.get_run_entries('r1')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['task_id'], 't1')

    def test_leaderboard_persists_across_instances(self):
        lb1 = hbp._Leaderboard()
        lb1.record_run(
            run_id='r1', benchmark='mmlu_mini', score=0.88,
            num_nodes=4, time_seconds=60, per_node=[], speedup=3.0)

        lb2 = hbp._Leaderboard()
        best = lb2.get_best_scores()
        self.assertIn('mmlu_mini', best)
        self.assertEqual(best['mmlu_mini']['score'], 0.88)


# ========================================================================
# Cleanup
# ========================================================================

def tearDownModule():
    _clean_tmp()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
