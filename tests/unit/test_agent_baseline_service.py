"""Tests for AgentBaselineService - unified performance snapshots."""
import json
import os
import shutil
import sys
import threading
import time

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.agent_engine.agent_baseline_service import (
    AgentBaselineService,
    AgentBaselineAdapter,
    capture_baseline_async,
    BASELINE_DIR,
    _recent_snapshots,
    _recent_lock,
    _DEDUP_WINDOW_S,
    _avg_success_rate,
)


@pytest.fixture(autouse=True)
def clean_baselines(tmp_path, monkeypatch):
    """Use tmp dir for baselines and clean up after each test."""
    test_dir = str(tmp_path / 'baselines')
    os.makedirs(test_dir, exist_ok=True)
    monkeypatch.setattr(
        'integrations.agent_engine.agent_baseline_service.BASELINE_DIR',
        test_dir)
    # Clear dedup state
    with _recent_lock:
        _recent_snapshots.clear()
    yield test_dir
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def sample_recipe(tmp_path):
    """Create a sample recipe JSON with experience data."""
    prompts_dir = tmp_path / 'prompts'
    prompts_dir.mkdir(exist_ok=True)
    recipe = {
        'actions': [
            {
                'action_id': 1,
                'action': 'Test action',
                'experience': {
                    'avg_duration_seconds': 2.5,
                    'success_rate': 0.95,
                    'run_count': 10,
                    'tool_stats': {'tool_a': 5},
                    'dead_ends': ['path1'],
                    'effective_fallbacks': ['fb1'],
                },
            },
            {
                'action_id': 2,
                'action': 'Another action',
                'experience': {
                    'avg_duration_seconds': 1.0,
                    'success_rate': 1.0,
                    'run_count': 10,
                    'tool_stats': {},
                    'dead_ends': [],
                    'effective_fallbacks': [],
                },
            },
        ],
        'experience_meta': {
            'total_runs': 10,
            'bottleneck_action_id': 1,
        },
    }
    recipe_path = prompts_dir / 'test_123_0_recipe.json'
    recipe_path.write_text(json.dumps(recipe))
    return str(prompts_dir.parent)


class TestCaptureSnapshot:
    """Test snapshot capture workflow."""

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_lightning_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_trust_evolution_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._build_metadata')
    def test_capture_creates_v1(
        self, mock_meta, mock_trust, mock_bench, mock_lightning,
        clean_baselines,
    ):
        mock_meta.return_value = {'trigger': 'creation'}
        mock_lightning.return_value = {'avg_reward': 0.5}
        mock_bench.return_value = {}
        mock_trust.return_value = {}

        snap = AgentBaselineService.capture_snapshot(
            'agent_a', 0, 'creation', 'user1')

        assert snap is not None
        assert snap['version'] == 1
        assert snap['prompt_id'] == 'agent_a'
        assert snap['flow_id'] == 0
        assert snap['trigger'] == 'creation'
        assert snap['lightning_metrics']['avg_reward'] == 0.5

        # Verify file on disk
        fpath = os.path.join(clean_baselines, 'agent_a_0', 'v1.json')
        assert os.path.isfile(fpath)

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_lightning_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_trust_evolution_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._build_metadata')
    def test_sequential_versions(
        self, mock_meta, mock_trust, mock_bench, mock_lightning,
        clean_baselines,
    ):
        mock_meta.return_value = {}
        mock_lightning.return_value = {}
        mock_bench.return_value = {}
        mock_trust.return_value = {}

        s1 = AgentBaselineService.capture_snapshot('b', 0, 'creation')
        # Use prompt_change to avoid dedup window
        s2 = AgentBaselineService.capture_snapshot('b', 0, 'prompt_change')

        assert s1['version'] == 1
        assert s2['version'] == 2

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_lightning_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_trust_evolution_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._build_metadata')
    def test_dedup_skips_recipe_change_after_creation(
        self, mock_meta, mock_trust, mock_bench, mock_lightning,
        clean_baselines,
    ):
        mock_meta.return_value = {}
        mock_lightning.return_value = {}
        mock_bench.return_value = {}
        mock_trust.return_value = {}

        s1 = AgentBaselineService.capture_snapshot('c', 0, 'creation')
        # Immediately call recipe_change - should be deduped
        s2 = AgentBaselineService.capture_snapshot('c', 0, 'recipe_change')

        assert s1 is not None
        assert s2 is None  # Deduped

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_lightning_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_trust_evolution_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._build_metadata')
    def test_recipe_change_after_dedup_window(
        self, mock_meta, mock_trust, mock_bench, mock_lightning,
        clean_baselines, monkeypatch,
    ):
        mock_meta.return_value = {}
        mock_lightning.return_value = {}
        mock_bench.return_value = {}
        mock_trust.return_value = {}

        # Simulate creation 120s ago (outside dedup window)
        with _recent_lock:
            _recent_snapshots['d_0'] = time.time() - 120

        # Recipe change should NOT be deduped (creation was >60s ago)
        s1 = AgentBaselineService.capture_snapshot('d', 0, 'recipe_change')
        assert s1 is not None
        assert s1['version'] == 1


class TestRecipeMetrics:
    """Test recipe metric collection."""

    def test_collect_recipe_metrics(self, sample_recipe, monkeypatch):
        prompts_dir = os.path.join(sample_recipe, 'prompts')
        monkeypatch.setattr(
            'integrations.agent_engine.agent_baseline_service.PROMPTS_DIR',
            prompts_dir)
        metrics = AgentBaselineService._collect_recipe_metrics('test_123', 0)

        assert metrics['action_count'] == 2
        assert metrics['total_expected_duration_seconds'] == 3.5
        assert metrics['total_runs'] == 10
        assert '1' in metrics['per_action']
        assert metrics['per_action']['1']['success_rate'] == 0.95
        assert metrics['per_action']['1']['dead_ends_count'] == 1

    def test_missing_recipe(self, tmp_path, monkeypatch):
        monkeypatch.chdir(str(tmp_path))
        metrics = AgentBaselineService._collect_recipe_metrics('missing', 0)
        assert metrics == {}


class TestVersionManagement:
    """Test get_latest, get_snapshot, list_snapshots."""

    def test_get_latest_snapshot(self, clean_baselines):
        # Write two versions
        agent_dir = os.path.join(clean_baselines, 'x_0')
        os.makedirs(agent_dir)
        for v in [1, 2]:
            fpath = os.path.join(agent_dir, f'v{v}.json')
            with open(fpath, 'w') as f:
                json.dump({'version': v, 'trigger': 'test'}, f)

        snap = AgentBaselineService.get_latest_snapshot('x', 0)
        assert snap is not None
        assert snap['version'] == 2

    def test_get_latest_no_data(self, clean_baselines):
        assert AgentBaselineService.get_latest_snapshot('nope', 0) is None

    def test_list_snapshots(self, clean_baselines):
        agent_dir = os.path.join(clean_baselines, 'y_1')
        os.makedirs(agent_dir)
        for v in [1, 2, 3]:
            with open(os.path.join(agent_dir, f'v{v}.json'), 'w') as f:
                json.dump({
                    'version': v,
                    'trigger': 'test',
                    'timestamp': 1000 + v,
                }, f)

        lst = AgentBaselineService.list_snapshots('y', 1)
        assert len(lst) == 3
        assert lst[0]['version'] == 1
        assert lst[2]['version'] == 3


class TestCompareSnapshots:
    """Test snapshot comparison and trend analysis."""

    def test_compare_snapshots(self, clean_baselines):
        agent_dir = os.path.join(clean_baselines, 'cmp_0')
        os.makedirs(agent_dir)

        for v, reward, dur in [(1, 0.5, 10.0), (2, 0.7, 8.0)]:
            with open(os.path.join(agent_dir, f'v{v}.json'), 'w') as f:
                json.dump({
                    'version': v,
                    'recipe_metrics': {
                        'action_count': 3,
                        'total_expected_duration_seconds': dur,
                        'total_runs': v * 5,
                    },
                    'lightning_metrics': {
                        'avg_reward': reward,
                        'error_rate': 0.1 / v,
                    },
                    'trust_evolution_metrics': {
                        'composite_trust': 2.0 + v * 0.5,
                        'generation': v,
                    },
                }, f)

        result = AgentBaselineService.compare_snapshots('cmp', 0, 1, 2)
        assert 'error' not in result
        assert result['lightning_delta']['avg_reward']['delta'] == 0.2
        assert result['lightning_delta']['avg_reward']['improved'] is True
        assert result['recipe_delta']['total_duration']['delta'] == -2.0
        assert result['recipe_delta']['total_duration']['improved'] is True

    def test_compute_trend_improving(self, clean_baselines):
        agent_dir = os.path.join(clean_baselines, 'trend_0')
        os.makedirs(agent_dir)

        for v in [1, 2]:
            with open(os.path.join(agent_dir, f'v{v}.json'), 'w') as f:
                json.dump({
                    'version': v,
                    'trigger': 'test',
                    'timestamp': 1000 + v,
                    'lightning_metrics': {'avg_reward': 0.3 + v * 0.2},
                    'recipe_metrics': {
                        'total_expected_duration_seconds': 10.0 - v * 2,
                    },
                }, f)

        trend = AgentBaselineService.compute_trend('trend', 0)
        assert trend['reward_trend'] == 'improving'

    def test_compute_trend_insufficient_data(self, clean_baselines):
        trend = AgentBaselineService.compute_trend('none', 0)
        assert trend['trend'] == 'insufficient_data'


class TestValidateAgainstBaseline:
    """Test CI/CD baseline validation."""

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_recipe_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    def test_passes_when_no_regression(
        self, mock_bench, mock_recipe, clean_baselines,
    ):
        # Create baseline
        agent_dir = os.path.join(clean_baselines, 'val_0')
        os.makedirs(agent_dir)
        with open(os.path.join(agent_dir, 'v1.json'), 'w') as f:
            json.dump({
                'version': 1,
                'recipe_metrics': {
                    'per_action': {
                        '1': {'success_rate': 0.90},
                    },
                },
                'benchmark_metrics': {},
            }, f)

        # Current metrics same or better
        mock_recipe.return_value = {
            'per_action': {'1': {'success_rate': 0.92}},
        }
        mock_bench.return_value = {}

        result = AgentBaselineService.validate_against_baseline('val', 0)
        assert result['passed'] is True
        assert len(result['regressions']) == 0

    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_recipe_metrics')
    @patch('integrations.agent_engine.agent_baseline_service.'
           'AgentBaselineService._collect_benchmark_metrics')
    def test_detects_regression(
        self, mock_bench, mock_recipe, clean_baselines,
    ):
        agent_dir = os.path.join(clean_baselines, 'reg_0')
        os.makedirs(agent_dir)
        with open(os.path.join(agent_dir, 'v1.json'), 'w') as f:
            json.dump({
                'version': 1,
                'recipe_metrics': {
                    'per_action': {
                        '1': {'success_rate': 0.90},
                    },
                },
                'benchmark_metrics': {},
            }, f)

        # Regression: success rate dropped
        mock_recipe.return_value = {
            'per_action': {'1': {'success_rate': 0.70}},
        }
        mock_bench.return_value = {}

        result = AgentBaselineService.validate_against_baseline('reg', 0)
        assert result['passed'] is False
        assert len(result['regressions']) > 0
        assert 'success_rate' in result['regressions'][0]


class TestAgentBaselineAdapter:
    """Test BenchmarkAdapter integration."""

    def test_adapter_runs(self, clean_baselines):
        # Create two versions for comparison
        agent_dir = os.path.join(clean_baselines, 'adapt_0')
        os.makedirs(agent_dir)
        for v, reward, dur in [(1, 0.5, 10.0), (2, 0.7, 8.0)]:
            with open(os.path.join(agent_dir, f'v{v}.json'), 'w') as f:
                json.dump({
                    'version': v,
                    'lightning_metrics': {'avg_reward': reward},
                    'recipe_metrics': {
                        'total_expected_duration_seconds': dur,
                        'per_action': {
                            '1': {'success_rate': 0.8 + v * 0.05},
                        },
                    },
                }, f)

        adapter = AgentBaselineAdapter()
        result = adapter.run()
        metrics = result.get('metrics', {})

        assert 'adapt_0_reward_delta' in metrics
        assert metrics['adapt_0_reward_delta']['value'] == 0.2
        assert 'adapt_0_duration_improvement_pct' in metrics

    def test_adapter_name_and_tier(self):
        adapter = AgentBaselineAdapter()
        assert adapter.name == 'agent_baselines'
        assert adapter.tier == 'fast'


class TestHelpers:
    """Test helper functions."""

    def test_avg_success_rate(self):
        pa = {
            '1': {'success_rate': 0.8},
            '2': {'success_rate': 1.0},
        }
        assert _avg_success_rate(pa) == 0.9

    def test_avg_success_rate_empty(self):
        assert _avg_success_rate({}) == 1.0

    def test_capture_baseline_async_does_not_block(self, clean_baselines):
        """Verify fire-and-forget returns immediately."""
        start = time.time()
        capture_baseline_async('async_test', 0, 'creation')
        elapsed = time.time() - start
        assert elapsed < 1.0  # Should return near-instantly
