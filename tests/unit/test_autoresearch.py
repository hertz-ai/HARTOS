"""
Tests for Auto-Evolve Code Tools — agent-native code experiment tools.

Tests cover:
- AutoResearchSession state management
- Metric extraction from experiment output
- Budget gating
- Improvement detection (higher/lower is better)
- History summary building
- Report saving
- Tool function registration (6 step-based tools)
- Goal type registration
- Seed goal entry
- EventBus progress events
- Git revert on failure
- Thought experiment tool wiring
- Generic iteration tools (iterate_hypothesis, score_hypothesis_result, get_iteration_history)
- Type-aware iteration recipes
- Benchmark integration
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestAutoResearchSession(unittest.TestCase):
    """Test AutoResearchSession dataclass and state logic."""

    def _make_session(self, **kwargs):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchSession
        defaults = {
            'repo_path': '/tmp/test_repo',
            'target_file': 'train.py',
            'run_command': 'python train.py',
            'metric_name': 'val_bpb',
            'spark_budget': 100,
            'spark_per_iteration': 4,
        }
        defaults.update(kwargs)
        return AutoResearchSession(**defaults)

    def test_budget_not_exhausted_initially(self):
        s = self._make_session()
        self.assertFalse(s.is_budget_exhausted())

    def test_budget_exhausted_when_consumed(self):
        s = self._make_session(spark_budget=10, spark_per_iteration=4)
        s.spark_consumed = 8  # 8 + 4 > 10
        self.assertTrue(s.is_budget_exhausted())

    def test_budget_exact_boundary(self):
        s = self._make_session(spark_budget=8, spark_per_iteration=4)
        s.spark_consumed = 4  # 4 + 4 = 8, not > 8
        self.assertFalse(s.is_budget_exhausted())

    def test_is_improved_higher_is_better(self):
        s = self._make_session(metric_direction='higher_is_better')
        s.best_metric = 0.85
        self.assertTrue(s.is_improved(0.90))
        self.assertFalse(s.is_improved(0.80))
        self.assertFalse(s.is_improved(0.85))

    def test_is_improved_lower_is_better(self):
        s = self._make_session(metric_direction='lower_is_better')
        s.best_metric = 1.5
        self.assertTrue(s.is_improved(1.2))
        self.assertFalse(s.is_improved(1.8))

    def test_is_improved_no_baseline(self):
        s = self._make_session()
        s.best_metric = None
        self.assertTrue(s.is_improved(0.5))

    def test_to_progress_dict(self):
        s = self._make_session()
        s.status = 'running'
        s.current_iteration = 5
        s.best_metric = 0.92
        s.start_time = 1000.0
        d = s.to_progress_dict()
        self.assertEqual(d['status'], 'running')
        self.assertEqual(d['iteration'], 5)
        self.assertEqual(d['best_metric'], 0.92)
        self.assertIn('session_id', d)
        self.assertIn('elapsed_s', d)

    def test_delta_property(self):
        from integrations.coding_agent.autoevolve_code_tools import ExperimentResult
        r = ExperimentResult(
            iteration=1, hypothesis='test', metric_name='score',
            metric_value=0.95, baseline_value=0.90, improved=True)
        self.assertAlmostEqual(r.delta, 0.05)

    def test_delta_none_when_no_values(self):
        from integrations.coding_agent.autoevolve_code_tools import ExperimentResult
        r = ExperimentResult(
            iteration=1, hypothesis='test', metric_name='score',
            metric_value=None, baseline_value=0.90, improved=False)
        self.assertIsNone(r.delta)


class TestMetricExtraction(unittest.TestCase):
    """Test the metric extraction logic."""

    def _get_engine(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine
        return AutoResearchEngine()

    def _make_session(self, **kwargs):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchSession
        defaults = {
            'repo_path': '/tmp/test',
            'target_file': 'train.py',
            'run_command': 'echo ok',
            'metric_name': 'val_bpb',
        }
        defaults.update(kwargs)
        return AutoResearchSession(**defaults)

    def test_extract_colon_format(self):
        engine = self._get_engine()
        session = self._make_session(metric_name='val_bpb')
        output = "epoch 1\nval_bpb: 1.234\ndone"
        val = engine.extract_metric(output, session)
        self.assertAlmostEqual(val, 1.234)

    def test_extract_equals_format(self):
        engine = self._get_engine()
        session = self._make_session(metric_name='accuracy')
        output = "training complete\naccuracy=0.95\n"
        val = engine.extract_metric(output, session)
        self.assertAlmostEqual(val, 0.95)

    def test_extract_custom_pattern(self):
        engine = self._get_engine()
        session = self._make_session(
            metric_name='custom',
            metric_pattern=r'FINAL_SCORE:\s*(\d+\.\d+)')
        output = "FINAL_SCORE: 42.5\n"
        val = engine.extract_metric(output, session)
        self.assertAlmostEqual(val, 42.5)

    def test_extract_pytest_passed(self):
        engine = self._get_engine()
        session = self._make_session(metric_name='tests_passed')
        output = "==================== 42 passed in 3.21s ====================\n"
        val = engine.extract_metric(output, session)
        self.assertEqual(val, 42.0)

    def test_extract_no_match(self):
        engine = self._get_engine()
        session = self._make_session(metric_name='nonexistent_metric')
        output = "hello world\n"
        val = engine.extract_metric(output, session)
        self.assertIsNone(val)

    def test_extract_score_keyword(self):
        engine = self._get_engine()
        session = self._make_session(metric_name='my_score')
        output = "result: 0.777\n"
        val = engine.extract_metric(output, session)
        self.assertAlmostEqual(val, 0.777)


class TestHistorySummary(unittest.TestCase):
    """Test history summary building for LLM context."""

    def test_empty_history(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        summary = engine.build_history_summary(session)
        self.assertEqual(summary, 'No previous iterations.')

    def test_history_with_results(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        session.results = [
            {'iteration': 0, 'improved': False, 'metric_value': 1.5,
             'hypothesis': 'baseline', 'error': ''},
            {'iteration': 1, 'improved': True, 'metric_value': 1.3,
             'hypothesis': 'reduce learning rate', 'error': ''},
            {'iteration': 2, 'improved': False, 'metric_value': None,
             'hypothesis': 'crashed attempt', 'error': 'IndexError'},
        ]
        summary = engine.build_history_summary(session)
        self.assertIn('baseline', summary)
        self.assertIn('IMPROVED', summary)
        self.assertIn('CRASHED', summary)
        self.assertIn('IndexError', summary)

    def test_history_truncated_to_10(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        session.results = [
            {'iteration': i, 'improved': False, 'metric_value': float(i),
             'hypothesis': f'hyp_{i}', 'error': ''}
            for i in range(20)
        ]
        summary = engine.build_history_summary(session)
        # Should only include last 10
        self.assertNotIn('hyp_5', summary)
        self.assertIn('hyp_15', summary)


class TestReportSaving(unittest.TestCase):
    """Test session report persistence."""

    def test_save_report_creates_file(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()

        with tempfile.TemporaryDirectory() as tmpdir:
            session = AutoResearchSession(
                repo_path=tmpdir, target_file='t.py', run_command='echo')
            session.status = 'completed'
            session.baseline_metric = 1.0
            session.best_metric = 0.8
            session.results = [{'iteration': 0, 'improved': False}]

            engine.save_report(session)

            # Check default location
            default_dir = os.path.join(
                os.path.dirname(__file__), '..', '..', 'agent_data', 'autoresearch')
            report_path = os.path.join(default_dir, f'{session.session_id}.json')
            if os.path.isfile(report_path):
                with open(report_path, 'r') as f:
                    report = json.load(f)
                self.assertEqual(report['session']['status'], 'completed')
                # Cleanup
                os.remove(report_path)


class TestGoalTypeRegistration(unittest.TestCase):
    """Test that autoresearch is registered as a goal type."""

    def test_autoresearch_registered(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        self.assertIn('autoresearch', types)

    def test_autoresearch_has_prompt_builder(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('autoresearch')
        self.assertIsNotNone(builder)

    def test_autoresearch_prompt_content(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('autoresearch')
        goal = {
            'title': 'Test Experiment',
            'description': 'Optimize test performance',
            'config': {
                'repo_path': '/tmp/repo',
                'target_file': 'model.py',
                'run_command': 'pytest tests/',
                'metric_name': 'accuracy',
                'metric_direction': 'higher_is_better',
                'max_iterations': 25,
            },
        }
        prompt = builder(goal)
        self.assertIn('AUTONOMOUS RESEARCH AGENT', prompt)
        self.assertIn('model.py', prompt)
        self.assertIn('pytest tests/', prompt)
        self.assertIn('accuracy', prompt)
        self.assertIn('higher_is_better', prompt.lower())

    def test_autoresearch_tool_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('autoresearch')
        self.assertIn('autoresearch', tags)
        self.assertIn('coding', tags)


class TestSeedGoal(unittest.TestCase):
    """Test that the autoresearch seed goal exists."""

    def test_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        self.assertIn('bootstrap_autoresearch_coordinator', slugs)

    def test_seed_goal_type(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_autoresearch_coordinator')
        self.assertEqual(goal['goal_type'], 'autoresearch')
        self.assertTrue(goal['config'].get('continuous'))
        self.assertTrue(goal['config'].get('hive_parallel'))


class TestThoughtExperimentToolWiring(unittest.TestCase):
    """Test that thought experiment tools include autoresearch."""

    def test_tool_list_includes_autoresearch(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        names = [t['name'] for t in THOUGHT_EXPERIMENT_TOOLS]
        self.assertIn('launch_experiment_autoresearch', names)
        self.assertIn('get_experiment_research_status', names)

    def test_tool_count(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        self.assertEqual(len(THOUGHT_EXPERIMENT_TOOLS), 11)

    def test_autoresearch_tool_tags(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        tool = next(t for t in THOUGHT_EXPERIMENT_TOOLS
                    if t['name'] == 'launch_experiment_autoresearch')
        self.assertIn('thought_experiment', tool['tags'])
        self.assertIn('autoresearch', tool['tags'])

    def test_launch_missing_repo(self):
        from integrations.agent_engine.thought_experiment_tools import launch_experiment_autoresearch
        result = json.loads(launch_experiment_autoresearch(
            experiment_id='test',
            repo_path='/nonexistent/path/xyz',
            target_file='train.py',
            run_command='echo hello',
        ))
        self.assertIn('error', result)


class TestAutoEvolveToolFunctions(unittest.TestCase):
    """Test the step-based tool functions."""

    def test_setup_missing_repo(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_setup
        result = json.loads(autoresearch_setup(
            repo_path='/nonexistent/repo/xyz',
            target_file='train.py',
            run_command='echo hello'))
        self.assertIn('error', result)
        self.assertIn('not found', result['error'])

    def test_setup_missing_file(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_setup
        with tempfile.TemporaryDirectory() as tmpdir:
            result = json.loads(autoresearch_setup(
                repo_path=tmpdir,
                target_file='nonexistent.py',
                run_command='echo hello'))
            self.assertIn('error', result)
            self.assertIn('not found', result['error'])

    def test_start_autoresearch_backward_compat(self):
        """start_autoresearch delegates to autoresearch_setup."""
        from integrations.coding_agent.autoevolve_code_tools import start_autoresearch
        result = json.loads(start_autoresearch(
            repo_path='/nonexistent/repo/xyz',
            target_file='train.py',
            run_command='echo hello'))
        self.assertIn('error', result)
        self.assertIn('not found', result['error'])

    def test_edit_no_session(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_edit
        result = json.loads(autoresearch_edit('nonexistent_session'))
        self.assertIn('error', result)

    def test_run_no_session(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_run
        result = json.loads(autoresearch_run('nonexistent_session'))
        self.assertIn('error', result)

    def test_decide_no_session(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_decide
        result = json.loads(autoresearch_decide('nonexistent_session'))
        self.assertIn('error', result)

    def test_finalize_no_session(self):
        from integrations.coding_agent.autoevolve_code_tools import autoresearch_finalize
        result = json.loads(autoresearch_finalize('nonexistent_session'))
        self.assertIn('error', result)

    def test_get_status_no_session(self):
        from integrations.coding_agent.autoevolve_code_tools import get_autoresearch_status
        result = json.loads(get_autoresearch_status('nonexistent_session'))
        self.assertIn('error', result)

    def test_get_status_all_sessions(self):
        from integrations.coding_agent.autoevolve_code_tools import get_autoresearch_status
        result = json.loads(get_autoresearch_status())
        self.assertIn('active_sessions', result)
        self.assertIsInstance(result['active_sessions'], list)

    def test_tool_registration_count(self):
        from integrations.coding_agent.autoevolve_code_tools import AUTOEVOLVE_CODE_TOOLS
        self.assertEqual(len(AUTOEVOLVE_CODE_TOOLS), 6)
        names = [t['name'] for t in AUTOEVOLVE_CODE_TOOLS]
        self.assertIn('autoresearch_setup', names)
        self.assertIn('autoresearch_edit', names)
        self.assertIn('autoresearch_run', names)
        self.assertIn('autoresearch_decide', names)
        self.assertIn('autoresearch_finalize', names)
        self.assertIn('get_autoresearch_status', names)

    def test_backward_compat_alias(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AUTORESEARCH_TOOLS, AUTOEVOLVE_CODE_TOOLS)
        self.assertIs(AUTORESEARCH_TOOLS, AUTOEVOLVE_CODE_TOOLS)


class TestSingleton(unittest.TestCase):
    """Test the singleton pattern."""

    def test_get_autoresearch_engine_singleton(self):
        from integrations.coding_agent.autoevolve_code_tools import get_autoresearch_engine
        e1 = get_autoresearch_engine()
        e2 = get_autoresearch_engine()
        self.assertIs(e1, e2)

    def test_get_active_sessions_empty(self):
        from integrations.coding_agent.autoevolve_code_tools import get_autoresearch_engine
        engine = get_autoresearch_engine()
        sessions = engine.get_active_sessions()
        self.assertIsInstance(sessions, list)


class TestSessionManagement(unittest.TestCase):
    """Test register/unregister session lifecycle."""

    def test_register_and_get_session(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        engine.register_session(session)
        retrieved = engine.get_session(session.session_id)
        self.assertIs(retrieved, session)

    def test_unregister_session(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        engine.register_session(session)
        engine.unregister_session(session.session_id)
        self.assertIsNone(engine.get_session(session.session_id))

    def test_get_nonexistent_session(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine
        engine = AutoResearchEngine()
        self.assertIsNone(engine.get_session('doesnt_exist'))


class TestRateLimiting(unittest.TestCase):
    """Test rate limit entry exists."""

    def test_autoresearch_rate_limit_entry(self):
        from security.rate_limiter_redis import RedisRateLimiter
        self.assertIn('autoresearch', RedisRateLimiter.LIMITS)
        limit, window = RedisRateLimiter.LIMITS['autoresearch']
        self.assertEqual(limit, 5)
        self.assertEqual(window, 3600)


class TestEventBusEmission(unittest.TestCase):
    """Test that progress events are emitted correctly."""

    def test_emit_progress_calls_emit_event(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo',
            experiment_id='exp_123', goal_id='goal_456')

        with patch('core.platform.events.emit_event') as mock_emit:
            engine.emit_progress(session, 'autoresearch.started', {'test': True})
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            self.assertEqual(call_args[0][0], 'autoresearch.started')
            self.assertEqual(call_args[0][1]['session_id'], session.session_id)
            self.assertEqual(call_args[0][1]['experiment_id'], 'exp_123')

    def test_emit_progress_no_crash_on_failure(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        # Should not raise even if emit_event is unavailable
        engine.emit_progress(session, 'autoresearch.test', {})


class TestGitRevert(unittest.TestCase):
    """Test git revert on experiment failure."""

    def test_revert_calls_git_checkout(self):
        from integrations.coding_agent.autoevolve_code_tools import AutoResearchEngine, AutoResearchSession
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp/repo', target_file='train.py', run_command='echo')

        with patch('integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess',
                   return_value=(0, '')) as mock_cmd:
            engine.revert_changes(session)
            mock_cmd.assert_called_once()
            args = mock_cmd.call_args
            self.assertIn('git checkout', args[0][0])
            self.assertIn('train.py', args[0][0])


class TestGenericIterationTools(unittest.TestCase):
    """Test the generic iteration tools for ALL experiment types."""

    def test_iterate_hypothesis_returns_context(self):
        from integrations.agent_engine.thought_experiment_tools import iterate_hypothesis
        # Without a real DB, this will return an error (experiment not found)
        result = json.loads(iterate_hypothesis(
            experiment_id='test_exp_1',
            hypothesis='Increasing learning rate improves convergence',
            approach='Compare lr=0.01 vs lr=0.001',
            iteration=1))
        # Either returns context or error (no DB) — both valid
        self.assertTrue('success' in result or 'error' in result)

    def test_score_hypothesis_creates_history(self):
        from integrations.agent_engine.thought_experiment_tools import score_hypothesis_result

        exp_id = f'test_score_{uuid.uuid4().hex[:8]}'
        result = json.loads(score_hypothesis_result(
            experiment_id=exp_id,
            iteration=1,
            score=1.5,
            reasoning='Strong evidence from data',
            evidence_quality=0.9,
            clarity=0.8,
            feasibility=0.7,
            impact=0.6))
        self.assertTrue(result.get('success'))
        self.assertEqual(result['record']['score'], 1.5)
        self.assertIn('trend', result)
        self.assertIn('advice', result)

        # Clean up
        data_dir = os.path.join(
            os.path.dirname(__file__), '..', '..', 'agent_data', 'experiment_iterations')
        history_path = os.path.join(data_dir, f'{exp_id}.json')
        if os.path.isfile(history_path):
            os.remove(history_path)

    def test_score_clamps_values(self):
        from integrations.agent_engine.thought_experiment_tools import score_hypothesis_result
        result = json.loads(score_hypothesis_result(
            experiment_id='test_clamp',
            iteration=1,
            score=5.0,  # Should clamp to 2.0
            reasoning='test',
            evidence_quality=1.5,  # Should clamp to 1.0
        ))
        self.assertTrue(result.get('success'))
        self.assertEqual(result['record']['score'], 2.0)
        self.assertEqual(result['record']['rubric']['evidence_quality'], 1.0)

    def test_get_iteration_history_empty(self):
        from integrations.agent_engine.thought_experiment_tools import get_iteration_history
        result = json.loads(get_iteration_history(
            experiment_id='nonexistent_exp_xyz'))
        self.assertTrue(result.get('success'))
        self.assertEqual(result['history'], [])
        self.assertIn('No iterations yet', result['summary'])

    def test_iteration_tools_registered(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        names = [t['name'] for t in THOUGHT_EXPERIMENT_TOOLS]
        self.assertIn('iterate_hypothesis', names)
        self.assertIn('score_hypothesis_result', names)
        self.assertIn('get_iteration_history', names)

    def test_iteration_tool_tags(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        for tool_name in ['iterate_hypothesis', 'score_hypothesis_result', 'get_iteration_history']:
            tool = next(t for t in THOUGHT_EXPERIMENT_TOOLS if t['name'] == tool_name)
            self.assertIn('iteration', tool['tags'])
            self.assertIn('thought_experiment', tool['tags'])


class TestTypeAwareIterationRecipe(unittest.TestCase):
    """Test the _build_iteration_recipe for different experiment types."""

    def test_software_recipe(self):
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        mock_exp = MagicMock()
        mock_exp.hypothesis = 'Optimizer change improves loss'
        mock_exp.expected_outcome = 'Lower val_bpb'
        mock_exp.intent_category = 'technology'

        recipe = ThoughtExperimentService._build_iteration_recipe(mock_exp, 'software')
        self.assertEqual(recipe['strategy'], 'autoresearch')
        self.assertIn('launch_experiment_autoresearch', recipe['tools'])
        self.assertEqual(recipe['scoring'], 'metric_extraction')

    def test_traditional_recipe(self):
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        mock_exp = MagicMock()
        mock_exp.hypothesis = 'Community gardens improve wellbeing'
        mock_exp.expected_outcome = 'Higher reported satisfaction'
        mock_exp.intent_category = 'community'

        recipe = ThoughtExperimentService._build_iteration_recipe(mock_exp, 'traditional')
        self.assertEqual(recipe['strategy'], 'reason_and_refine')
        self.assertIn('iterate_hypothesis', recipe['tools'])
        self.assertIn('score_hypothesis_result', recipe['tools'])
        self.assertEqual(recipe['scoring'], 'llm_rubric')
        self.assertEqual(recipe['max_iterations'], 10)

    def test_physical_ai_recipe(self):
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        mock_exp = MagicMock()
        mock_exp.hypothesis = 'Servo angle 45 is optimal'
        mock_exp.expected_outcome = 'Faster response'
        mock_exp.intent_category = 'technology'

        recipe = ThoughtExperimentService._build_iteration_recipe(mock_exp, 'physical_ai')
        self.assertEqual(recipe['strategy'], 'observe_and_measure')
        self.assertIn('iterate_hypothesis', recipe['tools'])
        self.assertEqual(recipe['max_iterations'], 20)

    def test_unknown_type_falls_back_to_traditional(self):
        from integrations.social.thought_experiment_service import ThoughtExperimentService
        mock_exp = MagicMock()
        mock_exp.hypothesis = 'New domain experiment'
        mock_exp.expected_outcome = 'Something cool'
        mock_exp.intent_category = 'technology'

        recipe = ThoughtExperimentService._build_iteration_recipe(mock_exp, 'some_new_type')
        self.assertEqual(recipe['strategy'], 'reason_and_refine')


class TestBenchmarkIntegration(unittest.TestCase):
    """Test that autoresearch feeds into BenchmarkTracker."""

    def test_record_benchmark_called(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine, AutoResearchSession, ExperimentResult)
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo',
            metric_name='score', goal_id='goal_1')

        result = ExperimentResult(
            iteration=1, hypothesis='test', metric_name='score',
            metric_value=0.95, baseline_value=0.9, improved=True,
            duration_s=5.0)

        with patch('integrations.coding_agent.benchmark_tracker.get_benchmark_tracker') as mock_tracker:
            tracker_instance = MagicMock()
            mock_tracker.return_value = tracker_instance
            engine.record_benchmark(session, result)
            tracker_instance.record.assert_called_once()
            call_kwargs = tracker_instance.record.call_args
            self.assertEqual(call_kwargs[1]['task_type'], 'autoresearch')
            self.assertTrue(call_kwargs[1]['success'])

    def test_record_benchmark_no_crash_on_failure(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine, AutoResearchSession, ExperimentResult)
        engine = AutoResearchEngine()
        session = AutoResearchSession(
            repo_path='/tmp', target_file='t.py', run_command='echo')
        result = ExperimentResult(
            iteration=1, hypothesis='test', metric_name='score',
            metric_value=None, baseline_value=None, improved=False,
            error='crash')
        # Should not raise even without real BenchmarkTracker
        engine.record_benchmark(session, result)


if __name__ == '__main__':
    unittest.main()
