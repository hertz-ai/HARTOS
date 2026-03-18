"""
Functional test: Auto-evolve closed loop verification.

Proves the autoresearch loop closes end-to-end:
  setup → (edit → run → decide) × N → finalize

Also tests the generic iteration loop (thought experiments):
  iterate_hypothesis → score → check trend → iterate again → converge
"""
import json
import os
import sys
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestAutoresearchClosedLoop(unittest.TestCase):
    """Verify the full setup → (edit → run → decide) × N → finalize cycle."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.target = os.path.join(self.tmpdir, 'model.py')
        with open(self.target, 'w') as f:
            f.write('accuracy = 0.70\n')

        self._iteration_metrics = []
        self._metric_index = 0

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_run_experiment(self, session, is_baseline=False):
        from integrations.coding_agent.autoevolve_code_tools import ExperimentResult
        if is_baseline:
            return ExperimentResult(
                iteration=0, hypothesis='baseline',
                metric_name=session.metric_name, metric_value=0.70,
                baseline_value=None, improved=False, duration_s=1.0)
        idx = self._metric_index
        self._metric_index += 1
        val = self._iteration_metrics[idx] if idx < len(self._iteration_metrics) else 0.70
        return ExperimentResult(
            iteration=session.current_iteration,
            hypothesis='test hypothesis',
            metric_name=session.metric_name, metric_value=val,
            baseline_value=session.best_metric,
            improved=session.is_improved(val), duration_s=1.0)

    def _run_loop(self, max_iterations, metrics, spark_budget=200):
        """Run the full autoresearch loop and return (decisions, final_report)."""
        self._iteration_metrics = metrics
        self._metric_index = 0

        from integrations.coding_agent.autoevolve_code_tools import (
            autoresearch_setup, autoresearch_edit, autoresearch_run,
            autoresearch_decide, autoresearch_finalize,
            get_autoresearch_engine, AutoResearchEngine)

        engine = get_autoresearch_engine()

        # Setup: mock run_experiment for baseline
        with patch.object(engine, 'run_experiment', side_effect=self._mock_run_experiment):
            setup = json.loads(autoresearch_setup(
                repo_path=self.tmpdir, target_file='model.py',
                run_command='echo accuracy: 0.70', metric_name='accuracy',
                metric_direction='higher_is_better',
                max_iterations=max_iterations))

        session_id = setup['session_id']
        session = engine.get_session(session_id)
        if spark_budget != 200:
            session.spark_budget = spark_budget

        # Mock edit generation (always succeeds)
        mock_edit = patch.object(engine, 'generate_and_apply_edit',
                                 return_value=('hypothesis', [{'s': 'a', 'r': 'b'}], ['model.py']))
        mock_commit = patch.object(engine, 'commit_improvement')
        mock_revert = patch.object(engine, 'revert_changes')
        mock_bench = patch.object(engine, 'record_benchmark')
        mock_event = patch.object(engine, 'emit_progress')

        decisions = []
        with mock_edit, mock_commit, mock_revert, mock_bench, mock_event:
            should_continue = True
            while should_continue:
                json.loads(autoresearch_edit(session_id))
                with patch.object(engine, 'run_experiment',
                                  side_effect=self._mock_run_experiment):
                    json.loads(autoresearch_run(session_id))
                result = json.loads(autoresearch_decide(session_id))
                decisions.append(result['decision'])
                should_continue = result['should_continue']

            with patch.object(engine, 'save_report'):
                final = json.loads(autoresearch_finalize(session_id))

        return decisions, final

    def test_3_iters_mixed_improve_regress(self):
        """iter1 improves, iter2 regresses (reverted), iter3 improves."""
        decisions, final = self._run_loop(
            max_iterations=3, metrics=[0.75, 0.68, 0.80])

        self.assertEqual(decisions, ['kept', 'reverted', 'kept'])
        self.assertEqual(final['total_improvements'], 2)
        self.assertEqual(final['best_metric'], 0.80)
        self.assertEqual(final['baseline_metric'], 0.70)
        self.assertEqual(final['total_iterations'], 3)

    def test_stops_on_budget(self):
        """Spark budget exhaustion stops the loop."""
        decisions, final = self._run_loop(
            max_iterations=10, metrics=[0.75, 0.80, 0.85, 0.90],
            spark_budget=12)  # 4/iter → 3 iters max

        self.assertEqual(len(decisions), 3)
        self.assertEqual(final['total_iterations'], 3)

    def test_stops_on_max_iterations(self):
        """max_iterations cap stops the loop."""
        decisions, final = self._run_loop(
            max_iterations=2, metrics=[0.75, 0.80])

        self.assertEqual(len(decisions), 2)
        self.assertEqual(final['total_iterations'], 2)

    def test_all_reverts(self):
        """Every iteration regresses — best stays at baseline."""
        decisions, final = self._run_loop(
            max_iterations=3, metrics=[0.60, 0.55, 0.50])

        self.assertEqual(decisions, ['reverted', 'reverted', 'reverted'])
        self.assertEqual(final['total_improvements'], 0)
        self.assertEqual(final['best_metric'], 0.70)


class TestGenericIterationClosedLoop(unittest.TestCase):
    """Verify the iterate_hypothesis → score → trend → converge cycle."""

    def setUp(self):
        self._tmproot = tempfile.mkdtemp()
        # Create the exact subpath that thought_experiment_tools expects
        self._history_dir = os.path.join(
            self._tmproot, 'agent_data', 'experiment_iterations')
        os.makedirs(self._history_dir, exist_ok=True)
        # The module computes data_dir as:
        #   os.path.join(os.path.dirname(__file__), '..', '..', 'agent_data', 'experiment_iterations')
        # So we fake __file__ to be <tmproot>/integrations/agent_engine/tools.py
        self._fake_file = os.path.join(
            self._tmproot, 'integrations', 'agent_engine', 'tools.py')
        os.makedirs(os.path.dirname(self._fake_file), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmproot, ignore_errors=True)

    def _run_iteration_loop(self, exp_id, scores):
        """Run N iterations with given scores, return list of advices."""
        from integrations.agent_engine.thought_experiment_tools import (
            iterate_hypothesis, score_hypothesis_result)

        mock_exp = {
            'id': exp_id, 'title': 'Test', 'hypothesis': 'H',
            'experiment_type': 'traditional', 'status': 'evaluating',
        }
        advices = []

        MockSvc = MagicMock()
        MockSvc.get_experiment_detail.return_value = mock_exp
        with patch('integrations.social.thought_experiment_service'
                    '.ThoughtExperimentService', MockSvc):
            with patch('integrations.social.models.db_session', MagicMock()):
                with patch('integrations.agent_engine.thought_experiment_tools'
                            '.__file__', self._fake_file):
                    for i, score in enumerate(scores):
                        json.loads(iterate_hypothesis(
                            experiment_id=exp_id,
                            hypothesis=f'H{i+1}', approach='A',
                            evidence='E', iteration=i + 1))
                        result = json.loads(score_hypothesis_result(
                            experiment_id=exp_id, iteration=i + 1,
                            score=score, reasoning=f'R{i+1}'))
                        advices.append(result.get('advice', ''))

        return advices

    def test_plateau_triggers_converge(self):
        """3 consecutive same scores → CONVERGE."""
        advices = self._run_iteration_loop(
            'exp-conv', [0.5, 1.0, 1.5, 1.5, 1.5])
        self.assertIn('CONVERGE', advices[-1])

    def test_improving_gives_continue(self):
        """Strictly improving scores → no CONVERGE."""
        advices = self._run_iteration_loop(
            'exp-imp', [0.5, 1.0, 1.5])
        self.assertNotIn('CONVERGE', advices[-1])

    def test_history_accumulates(self):
        """All iterations should be in history file."""
        from integrations.agent_engine.thought_experiment_tools import (
            iterate_hypothesis, score_hypothesis_result, get_iteration_history)

        exp_id = 'exp-hist'
        mock_exp = {
            'id': exp_id, 'title': 'T', 'hypothesis': 'H',
            'experiment_type': 'traditional', 'status': 'evaluating',
        }

        MockSvc = MagicMock()
        MockSvc.get_experiment_detail.return_value = mock_exp
        with patch('integrations.social.thought_experiment_service'
                    '.ThoughtExperimentService', MockSvc):
            with patch('integrations.social.models.db_session', MagicMock()):
                with patch('integrations.agent_engine.thought_experiment_tools'
                            '.__file__', self._fake_file):
                    for i, score in enumerate([0.5, 1.0, 1.5, 2.0]):
                        json.loads(iterate_hypothesis(
                            experiment_id=exp_id, hypothesis=f'H{i}',
                            approach='A', evidence='E', iteration=i + 1))
                        json.loads(score_hypothesis_result(
                            experiment_id=exp_id, iteration=i + 1,
                            score=score, reasoning='R'))

                    history = json.loads(get_iteration_history(
                        experiment_id=exp_id, last_n=10))

        self.assertEqual(history['summary']['total_iterations'], 4)
        self.assertEqual(history['summary']['best_score'], 2.0)

    def test_paused_experiment_blocks_iteration(self):
        """iterate_hypothesis should signal pause when experiment is paused."""
        from integrations.agent_engine.thought_experiment_tools import (
            iterate_hypothesis)
        import integrations.agent_engine.auto_evolve as ae_mod

        exp_id = 'exp-paused'
        ae_mod._paused_experiments[exp_id] = 'owner123'

        mock_exp = {
            'id': exp_id, 'title': 'T', 'hypothesis': 'H',
            'experiment_type': 'traditional', 'status': 'evaluating',
        }

        try:
            MockSvc = MagicMock()
            MockSvc.get_experiment_detail.return_value = mock_exp
            with patch('integrations.social.thought_experiment_service'
                        '.ThoughtExperimentService', MockSvc):
                with patch('integrations.social.models.db_session', MagicMock()):
                    result_str = iterate_hypothesis(
                        experiment_id=exp_id, hypothesis='H',
                        approach='A', evidence='E', iteration=1)
            result = json.loads(result_str)
            # Should contain pause signal somewhere in result
            result_text = json.dumps(result).lower()
            self.assertTrue(
                'pause' in result_text or 'stopped' in result_text
                or 'owner' in result_text,
                f"Expected pause signal in: {result_text[:200]}")
        finally:
            ae_mod._paused_experiments.pop(exp_id, None)


if __name__ == '__main__':
    unittest.main()
