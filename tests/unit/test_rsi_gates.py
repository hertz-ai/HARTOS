"""Tests for RSI-1 / RSI-2 / RSI-3 — recursive self-improvement loop closure.

The `commit_improvement` path is the "promotion" step of the autoresearch
self-improvement loop.  Before this change, a candidate passing the
in-session metric check was written to git + recipes + baseline snapshot
with no constitutional review and no cross-metric regression check, and
the learning delta was built but never broadcast to peer Hive nodes.

These tests verify the three closed-loop contracts:

    RSI-1  ConstitutionalFilter is invoked before any commit and, on
           rejection, the pending edits are reverted, counters increment,
           and nothing hits git / recipe / baseline.
    RSI-2  AgentBaselineService.validate_against_baseline is invoked
           before any commit and, on `passed=False`, the candidate is
           reverted with the regressions list recorded on the session.
    RSI-3  export_learning_delta calls FederatedAggregator.broadcast_delta
           so the improvement propagates to peer nodes.  If the aggregator
           is missing, federation_broadcast_enforced flips False with a
           WARNING (fail-loud, never fail-silent).

Fail-open rule: if a gate DEPENDENCY is missing (ImportError), the gate
allows the candidate through but flips the corresponding *_enforced flag
to False so dashboards see the bypass.  Fail-closed rule: guardrail
tamper is the one case that rejects.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def _make_session_and_result():
    from integrations.coding_agent.autoevolve_code_tools import (
        AutoResearchSession, ExperimentResult,
    )
    session = AutoResearchSession(
        repo_path='/tmp/x', target_file='t.py', run_command='echo',
        metric_name='score',
    )
    result = ExperimentResult(
        iteration=3, hypothesis='tune learning rate to 1e-3',
        metric_name='score', metric_value=0.92, baseline_value=0.80,
        improved=True, edits=[], files_changed=['t.py'],
    )
    return session, result


# ── RSI-1: ConstitutionalFilter gate ─────────────────────────────


class TestConstitutionalGate(unittest.TestCase):
    """RSI-1: every promoted improvement passes ConstitutionalFilter."""

    def test_rejection_reverts_and_does_not_commit(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, result = _make_session_and_result()

        with patch(
            'security.hive_guardrails.ConstitutionalFilter.check_prompt',
            return_value=(False, 'Constitutional violation: test-pattern'),
        ), patch.object(engine, 'revert_changes') as mock_revert, patch(
            'integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess'
        ) as mock_subproc, patch(
            'integrations.coding_agent.recipe_bridge.CodingRecipeBridge'
        ) as mock_bridge:
            committed = engine.commit_improvement(session, result)

        self.assertFalse(committed, 'gate rejection must return False')
        self.assertEqual(session.constitutional_rejections, 1)
        self.assertIn('constitutional', session.last_rejection_reason)
        mock_revert.assert_called_once_with(session)
        # Git commit and recipe capture MUST NOT run on rejection.
        mock_subproc.assert_not_called()
        mock_bridge.assert_not_called()

    def test_guardrail_tamper_fails_closed(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, result = _make_session_and_result()

        with patch(
            'security.hive_guardrails.ConstitutionalFilter.check_prompt',
            side_effect=RuntimeError('Guardrail integrity violated'),
        ), patch.object(engine, 'revert_changes') as mock_revert:
            committed = engine.commit_improvement(session, result)

        self.assertFalse(committed, 'tamper MUST fail-closed')
        self.assertEqual(session.constitutional_rejections, 1)
        self.assertIn('tamper', session.last_rejection_reason.lower())
        mock_revert.assert_called_once()

    def test_import_error_flips_flag_and_allows(self):
        """Missing module is fail-open with a loud flag flip."""
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, result = _make_session_and_result()

        original_import = __builtins__['__import__'] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == 'security.hive_guardrails':
                raise ImportError('module missing')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=_blocked_import), \
             patch(
                'integrations.agent_engine.agent_baseline_service.'
                'AgentBaselineService.validate_against_baseline',
                return_value={'passed': True, 'regressions': []},
        ), patch(
            'integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess',
            return_value=(0, ''),
        ), patch(
            'integrations.coding_agent.recipe_bridge.CodingRecipeBridge'
        ), patch(
            'integrations.agent_engine.agent_baseline_service.'
            'AgentBaselineService.capture_snapshot',
            return_value={},
        ):
            committed = engine.commit_improvement(session, result)

        self.assertTrue(committed, 'fail-open: missing gate must not block')
        self.assertFalse(session.constitutional_enforced,
                         'missing gate must flip the enforcement flag')


# ── RSI-2: baseline delta gate ───────────────────────────────────


class TestBaselineDeltaGate(unittest.TestCase):
    """RSI-2: no candidate is promoted if validate_against_baseline reports
    regressions — guards against one-metric-up-other-metric-down drift."""

    def test_regression_reverts_and_does_not_commit(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, result = _make_session_and_result()

        with patch(
            'security.hive_guardrails.ConstitutionalFilter.check_prompt',
            return_value=(True, 'ok'),
        ), patch(
            'integrations.agent_engine.agent_baseline_service.'
            'AgentBaselineService.validate_against_baseline',
            return_value={
                'passed': False,
                'regressions': ['action_5_success_rate: 0.92 -> 0.80'],
                'baseline_version': 7,
            },
        ), patch.object(engine, 'revert_changes') as mock_revert, patch(
            'integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess'
        ) as mock_subproc:
            committed = engine.commit_improvement(session, result)

        self.assertFalse(committed, 'regression must block promotion')
        self.assertEqual(session.baseline_rejections, 1)
        self.assertIn('baseline_regression', session.last_rejection_reason)
        self.assertIn('action_5_success_rate', session.last_rejection_reason)
        mock_revert.assert_called_once_with(session)
        mock_subproc.assert_not_called()

    def test_no_baseline_is_fail_open_first_run(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, result = _make_session_and_result()

        with patch(
            'security.hive_guardrails.ConstitutionalFilter.check_prompt',
            return_value=(True, 'ok'),
        ), patch(
            'integrations.agent_engine.agent_baseline_service.'
            'AgentBaselineService.validate_against_baseline',
            return_value={
                'passed': True, 'regressions': [],
                'reason': 'no baseline to compare',
            },
        ), patch(
            'integrations.coding_agent.aider_core.run_cmd.run_cmd_subprocess',
            return_value=(0, ''),
        ), patch(
            'integrations.coding_agent.recipe_bridge.CodingRecipeBridge'
        ), patch(
            'integrations.agent_engine.agent_baseline_service.'
            'AgentBaselineService.capture_snapshot',
            return_value={},
        ):
            committed = engine.commit_improvement(session, result)

        self.assertTrue(committed, 'first-run (no baseline) must promote')
        self.assertEqual(session.baseline_rejections, 0)


# ── RSI-3: federation broadcast ──────────────────────────────────


class TestFederationBroadcast(unittest.TestCase):
    """RSI-3: export_learning_delta actually transmits via
    FederatedAggregator.broadcast_delta — not just logs 'delta prepared'."""

    def test_broadcast_delta_is_called_with_autoresearch_payload(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, _ = _make_session_and_result()
        session.total_improvements = 4
        session.baseline_metric = 0.5
        session.best_metric = 0.82

        mock_aggregator = MagicMock()
        with patch(
            'integrations.coding_agent.benchmark_tracker.get_benchmark_tracker'
        ) as mock_tracker, patch(
            'integrations.agent_engine.federated_aggregator.'
            'get_federated_aggregator',
            return_value=mock_aggregator,
        ):
            mock_tracker.return_value.export_learning_delta.return_value = {
                'coding_benchmarks': {'mbpp': 0.77},
            }
            engine.export_learning_delta(session)

        mock_aggregator.broadcast_delta.assert_called_once()
        sent = mock_aggregator.broadcast_delta.call_args[0][0]
        self.assertIn('autoresearch', sent)
        self.assertEqual(sent['autoresearch']['total_improvements'], 4)
        self.assertEqual(sent['autoresearch']['best'], 0.82)
        self.assertTrue(session.federation_broadcast_enforced)

    def test_aggregator_missing_flips_flag_and_warns(self):
        from integrations.coding_agent.autoevolve_code_tools import (
            AutoResearchEngine,
        )
        engine = AutoResearchEngine()
        session, _ = _make_session_and_result()

        original_import = __builtins__['__import__'] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == 'integrations.agent_engine.federated_aggregator':
                raise ImportError('aggregator missing')
            return original_import(name, *args, **kwargs)

        with patch(
            'integrations.coding_agent.benchmark_tracker.get_benchmark_tracker'
        ) as mock_tracker, patch(
            'builtins.__import__', side_effect=_blocked_import,
        ):
            mock_tracker.return_value.export_learning_delta.return_value = {}
            with self.assertLogs('hevolve.autoresearch',
                                  level='WARNING') as lc:
                engine.export_learning_delta(session)

        self.assertFalse(session.federation_broadcast_enforced)
        self.assertIn('FederatedAggregator', '\n'.join(lc.output))


# ── Progress dict must surface all RSI flags ─────────────────────


class TestProgressDictContract(unittest.TestCase):
    def test_progress_dict_includes_rsi_flags(self):
        session, _ = _make_session_and_result()
        d = session.to_progress_dict()
        for key in (
            'constitutional_enforced', 'baseline_delta_enforced',
            'federation_broadcast_enforced',
            'constitutional_rejections', 'baseline_rejections',
            'last_rejection_reason',
        ):
            self.assertIn(key, d, f'progress dict missing RSI flag: {key}')


if __name__ == '__main__':
    unittest.main()
