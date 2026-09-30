"""The upgrade gate does not compare per-process counters.

WorldModelAdapter reported WorldModelBridge's cumulative in-memory counters
(total_corrections as 'correction_density', total_hivemind_queries) as
'higher is better' metrics, and is_upgrade_safe compared them across two
snapshots. Those counters restart at 0 with the process, so a restart between
the baseline and the candidate snapshot read as a regression and blocked the
upgrade (measured: baseline 5, candidate 0 -> blocked, on the same code).
They stay in the snapshot for display and federation; they no longer gate.

These tests run the real WorldModelAdapter, capture_snapshot and
is_upgrade_safe; only the bridge's stats are stubbed.
"""
import json
import os
import threading

import pytest

import integrations.agent_engine.benchmark_registry as br
import integrations.agent_engine.world_model_bridge as wmb


class _Bridge:
    def __init__(self, **stats):
        self.stats = stats

    def get_stats(self):
        return dict(self.stats)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(br, 'BENCHMARK_DIR', str(tmp_path))
    reg = br.BenchmarkRegistry.__new__(br.BenchmarkRegistry)
    reg._lock = threading.Lock()
    reg._adapters = {}
    reg._latest_results = {}
    reg.register_benchmark(br.WorldModelAdapter())

    def snapshot(version, **stats):
        monkeypatch.setattr(wmb, 'get_world_model_bridge',
                            lambda: _Bridge(**stats))
        return reg.capture_snapshot(version, tier='fast')
    return reg, snapshot, tmp_path


BUSY = dict(total_recorded=10, total_flushed=10,
            total_corrections=5, total_hivemind_queries=7)
RESTARTED = dict(total_recorded=10, total_flushed=10,
                 total_corrections=0, total_hivemind_queries=0)


def test_a_restart_between_snapshots_does_not_block_the_upgrade(registry):
    reg, snapshot, _ = registry
    snapshot('v1', **BUSY)
    snapshot('v2', **RESTARTED)
    safe, reason = reg.is_upgrade_safe('v1', 'v2')
    assert safe is True, reason


def test_a_baseline_written_before_the_fix_does_not_block_either(registry):
    """An old snapshot on disk carries the counters with no gate marker."""
    reg, snapshot, tmp = registry
    old = {'version': 'v1', 'benchmarks': {'world_model': {'metrics': {
        'flush_rate': {'value': 1.0, 'direction': 'higher', 'unit': 'ratio'},
        'correction_density': {'value': 5, 'direction': 'higher', 'unit': 'count'},
        'hivemind_queries': {'value': 7, 'direction': 'higher', 'unit': 'count'},
    }}}}
    (tmp / 'v1.json').write_text(json.dumps(old))
    snapshot('v2', **RESTARTED)
    safe, reason = reg.is_upgrade_safe('v1', 'v2')
    assert safe is True, reason


def test_a_candidate_from_an_older_build_does_not_block_either(registry):
    """A marked baseline against an unmarked candidate (a rollback to a build
    from before the fix): the marker on either side takes the metric out."""
    reg, snapshot, tmp = registry
    snapshot('v2', **BUSY)
    old_build = {'version': 'v1', 'benchmarks': {'world_model': {'metrics': {
        'flush_rate': {'value': 1.0, 'direction': 'higher', 'unit': 'ratio'},
        'correction_density': {'value': 0, 'direction': 'higher', 'unit': 'count'},
        'hivemind_queries': {'value': 0, 'direction': 'higher', 'unit': 'count'},
    }}}}
    (tmp / 'v1.json').write_text(json.dumps(old_build))
    safe, reason = reg.is_upgrade_safe('v2', 'v1')
    assert safe is True, reason


def test_the_counters_are_still_reported(registry):
    reg, snapshot, _ = registry
    snap = snapshot('v1', **BUSY)
    metrics = snap['benchmarks']['world_model']['metrics']
    assert metrics['correction_density']['value'] == 5
    assert metrics['hivemind_queries']['value'] == 7
    assert metrics['correction_density']['gate'] is False
    assert metrics['hivemind_queries']['gate'] is False
    assert 'gate' not in metrics['flush_rate']


def test_a_real_ratio_regression_still_blocks(registry):
    reg, snapshot, _ = registry
    snapshot('v1', **BUSY)
    snapshot('v2', **dict(RESTARTED, total_flushed=2))
    safe, reason = reg.is_upgrade_safe('v1', 'v2')
    assert safe is False
    assert 'world_model.flush_rate' in reason
    assert 'correction_density' not in reason


@pytest.mark.parametrize('marker', [False, 0, None, 'false'])
def test_only_an_explicit_false_turns_the_gate_off(tmp_path, monkeypatch, marker):
    """A metric leaves the gate only on a literal gate=False."""
    monkeypatch.setattr(br, 'BENCHMARK_DIR', str(tmp_path))
    reg = br.BenchmarkRegistry.__new__(br.BenchmarkRegistry)
    for v, value in (('v1', 100), ('v2', 50)):
        m = {'value': value, 'direction': 'higher', 'unit': 'pts', 'gate': marker}
        (tmp_path / f'{v}.json').write_text(json.dumps(
            {'benchmarks': {'b': {'metrics': {'score': m}}}}))
    safe, _ = reg.is_upgrade_safe('v1', 'v2')
    assert safe is (marker is False)
