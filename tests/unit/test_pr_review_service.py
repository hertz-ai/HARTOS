"""Tests for PRReviewService - automated PR review + build breaker detection."""
import json
import os
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.agent_engine.pr_review_service import PRReviewService


class TestClassifyChange:
    """Test change complexity classification."""

    def test_simple_change(self):
        assert PRReviewService.classify_change(
            {'files_changed': 2, 'additions': 30, 'deletions': 10}) == 'simple'

    def test_moderate_change(self):
        assert PRReviewService.classify_change(
            {'files_changed': 7, 'additions': 200, 'deletions': 100}) == 'moderate'

    def test_complex_change_many_files(self):
        assert PRReviewService.classify_change(
            {'files_changed': 15, 'additions': 50, 'deletions': 20}) == 'complex'

    def test_complex_change_many_lines(self):
        assert PRReviewService.classify_change(
            {'files_changed': 3, 'additions': 400, 'deletions': 200}) == 'complex'

    def test_empty_diff(self):
        assert PRReviewService.classify_change({}) == 'simple'


class TestPreCommitChecks:
    """Test pre-commit check runner."""

    @patch('subprocess.run')
    def test_passes_when_clean(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        result = PRReviewService.run_pre_commit_checks()
        assert result['passed'] is True
        assert len(result['issues']) == 0

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_passes_when_no_ruff(self, mock_run):
        result = PRReviewService.run_pre_commit_checks()
        assert result['passed'] is True


class TestRunTestSuite:
    """Test test suite runner."""

    @patch('subprocess.run')
    def test_parses_passing_results(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='=== 100 passed in 30.5s ===',
            stderr='',
        )
        result = PRReviewService.run_test_suite()
        assert result['passed'] == 100
        assert result['pass_rate'] == 1.0

    @patch('subprocess.run')
    def test_parses_mixed_results(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout='=== 95 passed, 5 failed in 45.0s ===',
            stderr='',
        )
        result = PRReviewService.run_test_suite()
        assert result['passed'] == 95
        assert result['failed'] == 5
        assert result['pass_rate'] == 0.95

    @patch('subprocess.run', side_effect=Exception('timeout'))
    def test_handles_error(self, mock_run):
        result = PRReviewService.run_test_suite()
        assert result['pass_rate'] == 0.0
        assert 'error' in result


class TestValidateBaseline:
    """Test baseline validation for all agents."""

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService.validate_against_baseline')
    def test_passes_with_no_regressions(self, mock_validate, tmp_path, monkeypatch):
        # Create a baselines directory with one agent
        baselines = tmp_path / 'baselines'
        agent_dir = baselines / 'test_0'
        agent_dir.mkdir(parents=True)
        (agent_dir / 'v1.json').write_text('{}')

        monkeypatch.setattr(
            'integrations.agent_engine.agent_baseline_service.BASELINE_DIR',
            str(baselines))

        mock_validate.return_value = {
            'passed': True, 'regressions': []}

        result = PRReviewService.validate_baseline()
        assert result['passed'] is True

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService.validate_against_baseline')
    def test_detects_regressions(self, mock_validate, tmp_path, monkeypatch):
        baselines = tmp_path / 'baselines'
        agent_dir = baselines / 'test_0'
        agent_dir.mkdir(parents=True)
        (agent_dir / 'v1.json').write_text('{}')

        monkeypatch.setattr(
            'integrations.agent_engine.agent_baseline_service.BASELINE_DIR',
            str(baselines))

        mock_validate.return_value = {
            'passed': False,
            'regressions': ['action_1_success_rate: 0.95 → 0.70']}

        result = PRReviewService.validate_baseline()
        assert result['passed'] is False
        assert len(result['regressions']) > 0


class TestReviewPR:
    """Test full PR review pipeline decisions."""

    @patch.object(PRReviewService, 'post_review')
    @patch.object(PRReviewService, '_fetch_pr_diff')
    @patch.object(PRReviewService, 'run_pre_commit_checks')
    @patch.object(PRReviewService, 'run_test_suite')
    @patch.object(PRReviewService, 'validate_baseline')
    def test_auto_approve_simple_passing(
        self, mock_baseline, mock_tests, mock_precommit,
        mock_diff, mock_post,
    ):
        mock_diff.return_value = {
            'files_changed': 2, 'additions': 20, 'deletions': 5}
        mock_precommit.return_value = {'passed': True, 'issues': []}
        mock_tests.return_value = {
            'passed': 100, 'failed': 0, 'pass_rate': 1.0}
        mock_baseline.return_value = {'passed': True, 'regressions': []}

        result = PRReviewService.review_pr('hevolve-ai/repo', 42)

        assert result['decision'] == 'approve'
        assert 'Simple change' in result['reason']

    @patch.object(PRReviewService, 'post_review')
    @patch.object(PRReviewService, '_fetch_pr_diff')
    @patch.object(PRReviewService, 'run_pre_commit_checks')
    @patch.object(PRReviewService, 'run_test_suite')
    @patch.object(PRReviewService, 'validate_baseline')
    def test_flag_steward_complex(
        self, mock_baseline, mock_tests, mock_precommit,
        mock_diff, mock_post,
    ):
        mock_diff.return_value = {
            'files_changed': 12, 'additions': 500, 'deletions': 200}
        mock_precommit.return_value = {'passed': True, 'issues': []}
        mock_tests.return_value = {
            'passed': 100, 'failed': 0, 'pass_rate': 1.0}
        mock_baseline.return_value = {'passed': True, 'regressions': []}

        result = PRReviewService.review_pr('hevolve-ai/repo', 43)

        assert result['decision'] == 'flag_steward'

    @patch.object(PRReviewService, 'post_review')
    @patch.object(PRReviewService, '_fetch_pr_diff')
    @patch.object(PRReviewService, 'run_pre_commit_checks')
    @patch.object(PRReviewService, 'run_test_suite')
    @patch.object(PRReviewService, 'validate_baseline')
    def test_build_breaker_tests_fail(
        self, mock_baseline, mock_tests, mock_precommit,
        mock_diff, mock_post,
    ):
        mock_diff.return_value = {
            'files_changed': 1, 'additions': 5, 'deletions': 2}
        mock_precommit.return_value = {'passed': True, 'issues': []}
        mock_tests.return_value = {
            'passed': 80, 'failed': 20, 'pass_rate': 0.80}
        mock_baseline.return_value = {'passed': True, 'regressions': []}

        result = PRReviewService.review_pr('hevolve-ai/repo', 44)

        assert result['decision'] == 'request_changes'
        assert 'Build breaker' in result['reason']

    @patch.object(PRReviewService, 'post_review')
    @patch.object(PRReviewService, '_fetch_pr_diff')
    @patch.object(PRReviewService, 'run_pre_commit_checks')
    @patch.object(PRReviewService, 'run_test_suite')
    @patch.object(PRReviewService, 'validate_baseline')
    def test_build_breaker_baseline_regression(
        self, mock_baseline, mock_tests, mock_precommit,
        mock_diff, mock_post,
    ):
        mock_diff.return_value = {
            'files_changed': 1, 'additions': 5, 'deletions': 2}
        mock_precommit.return_value = {'passed': True, 'issues': []}
        mock_tests.return_value = {
            'passed': 100, 'failed': 0, 'pass_rate': 1.0}
        mock_baseline.return_value = {
            'passed': False,
            'regressions': ['action_1_success_rate: 0.95 → 0.70']}

        result = PRReviewService.review_pr('hevolve-ai/repo', 45)

        assert result['decision'] == 'request_changes'
        assert 'regression' in result['reason'].lower()

    @patch.object(PRReviewService, '_fetch_pr_diff')
    def test_error_on_diff_failure(self, mock_diff):
        mock_diff.return_value = {'error': 'API timeout'}
        result = PRReviewService.review_pr('hevolve-ai/repo', 99)
        assert result['decision'] == 'error'
