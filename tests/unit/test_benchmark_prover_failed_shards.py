"""A benchmark run where nothing completed must not become a score of 0.0.

Regression test for the failure that hid a dead LLM credential for weeks: every
shard failed, _aggregate_results averaged them into 0.0 at full weight, and the
prover published that as a HIVE BENCHMARK PROOF on its normal schedule. Output
looked identical to a working system; the only difference was the Azure bill
falling to nearly nothing.

"the hive answered and got everything wrong" and "the hive never answered" are
different claims. Only the first is a score.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


@pytest.fixture()
def aggregate():
    from integrations.agent_engine.hive_benchmark_prover import HiveBenchmarkProver
    return HiveBenchmarkProver.__dict__['_aggregate_results']


def _shard(status, score=0.0, solved=0, total=10, node='n1'):
    return {'node_id': node, 'shard_index': 0, 'status': status,
            'score': score, 'problems_solved': solved, 'problems_total': total,
            'time_seconds': 1.0}


def test_all_shards_failed_yields_no_score(aggregate):
    """Nothing completed -> no score at all, and a stated reason."""
    out = aggregate(None, [_shard('failed'), _shard('failed', node='n2')])
    assert out['valid'] is False
    assert out['score'] is None, "a failed run must not report 0.0 as its score"
    assert out['failure_reason']
    assert out['nodes_completed'] == 0


def test_genuine_zero_is_still_a_score(aggregate):
    """Answered everything and got it wrong -> that IS 0.0, and stays valid."""
    out = aggregate(None, [_shard('completed', score=0.0, solved=0, total=10)])
    assert out['valid'] is True
    assert out['score'] == 0.0, "a real zero must survive; it is a legitimate result"
    assert out['nodes_completed'] == 1


def test_failed_shard_does_not_drag_down_a_good_one(aggregate):
    """A dead node must not dilute the score of a node that worked."""
    out = aggregate(None, [
        _shard('completed', score=0.8, solved=8, total=10),
        _shard('failed', score=0.0, solved=0, total=10, node='n2'),
    ])
    assert out['valid'] is True
    # Only the completed shard counts: 0.8, not the 0.4 a full-weight average gives.
    assert out['score'] == pytest.approx(0.8), (
        "failed shards were being averaged in at full weight")
    assert out['nodes_completed'] == 1
    assert out['num_nodes'] == 2, "per-node visibility of the failure is kept"


def test_empty_shard_list_is_invalid(aggregate):
    out = aggregate(None, [])
    assert out['valid'] is False
    assert out['score'] is None


def test_per_node_still_reports_failed_shards(aggregate):
    """Suppressing the score must not hide which nodes failed."""
    out = aggregate(None, [_shard('failed'), _shard('completed', score=0.5, solved=5)])
    statuses = sorted(n['status'] for n in out['per_node'])
    assert statuses == ['completed', 'failed']
