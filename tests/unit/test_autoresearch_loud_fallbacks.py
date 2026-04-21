"""Tests for Fix 2 — autoresearch_loop regression escape-hatches are LOUD.

When AgentBaselineService / BenchmarkTracker are unavailable the session
runs, but the corresponding `*_enforced` flag on AutoResearchSession must
flip to False AND a WARNING must be emitted.  No silent pass-through.
"""
import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestBaselineEnforcementFlag(unittest.TestCase):
    def setUp(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchSession, AutoResearchEngine, ExperimentResult,
        )
        self.session = AutoResearchSession(
            repo_path='/tmp/x', target_file='t.py', run_command='echo',
        )
        self.engine = AutoResearchEngine()
        # Fake result the commit_improvement expects
        self.result = ExperimentResult(
            iteration=3, hypothesis='h', metric_name='score',
            metric_value=0.9, baseline_value=0.8, improved=True,
        )

    def test_flags_default_true(self):
        """Fresh sessions assume enforcement is active."""
        self.assertTrue(self.session.baseline_enforced)
        self.assertTrue(self.session.benchmark_gain_enforced)
        self.assertTrue(self.session.federation_export_enforced)

    def test_baseline_service_missing_sets_flag_and_warns(self):
        """If AgentBaselineService raises ImportError, flag flips and WARN."""
        # Suppress the side-effect subprocess git call by patching run_cmd
        with patch(
            'integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess',
            return_value=(0, '')
        ), patch(
            'integrations.coding_agent.recipe_bridge.CodingRecipeBridge'
        ), patch(
            'integrations.agent_engine.agent_baseline_service.'
            'AgentBaselineService.capture_snapshot',
            side_effect=ImportError('module missing'),
        ):
            with self.assertLogs('hevolve.autoresearch',
                                  level='WARNING') as lc:
                self.engine.commit_improvement(self.session, self.result)

        self.assertFalse(self.session.baseline_enforced)
        joined = '\n'.join(lc.output)
        self.assertIn('AgentBaselineService', joined)
        self.assertIn('baseline_enforced=False', joined)

    def test_benchmark_tracker_missing_sets_flag_and_warns(self):
        with patch(
            'integrations.coding_agent.benchmark_tracker.get_benchmark_tracker',
            side_effect=ImportError('tracker missing'),
        ):
            with self.assertLogs('hevolve.autoresearch',
                                  level='WARNING') as lc:
                self.engine.record_benchmark(self.session, self.result)
        self.assertFalse(self.session.benchmark_gain_enforced)
        self.assertIn('BenchmarkTracker', '\n'.join(lc.output))

    def test_federation_export_missing_sets_flag_and_warns(self):
        with patch(
            'integrations.coding_agent.benchmark_tracker.get_benchmark_tracker',
            side_effect=ImportError('tracker gone'),
        ):
            with self.assertLogs('hevolve.autoresearch',
                                  level='WARNING') as lc:
                self.engine.export_learning_delta(self.session)
        self.assertFalse(self.session.federation_export_enforced)
        joined = '\n'.join(lc.output)
        self.assertIn('federation_export_enforced=False', joined)

    def test_progress_dict_includes_flags(self):
        """Dashboards must be able to read the enforcement state."""
        d = self.session.to_progress_dict()
        self.assertIn('baseline_enforced', d)
        self.assertIn('benchmark_gain_enforced', d)
        self.assertIn('federation_export_enforced', d)


if __name__ == '__main__':
    unittest.main()
