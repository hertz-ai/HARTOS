"""A benchmark whose problems have no answer key is not a benchmark it failed.

Companion to test_benchmark_prover_failed_shards.py, which established that "the
hive never answered" is not a score of 0.0. This file covers the case that
actually happened in production for 70 days: the hive DID answer, every shard
completed cleanly, and the score was still meaningless — because _fetch_problems
generates synthetic stubs ("Multiple choice question #1. Evaluate using hive
context.") that carry no `correct_answer`. Every answer was compared against ''
and graded wrong, so each run produced a tidy, plausible 0.0.

Measured consequence on the live instance, 2026-08-12: 416 of 1,267 posts in the
social feed were "HIVE BENCHMARK PROOF — 0.0%", published on schedule against a
comparison table of real published Claude / GPT / Gemini scores, and each one
spawned a thought experiment asking the community to vote on the result. 832
experiments accumulated from 16 distinct titles; one appeared 416 times; not one
vote was ever cast.

Three different claims, only the first of which is a measurement:
  "scored 0.0"   — answered, got everything wrong
  "no score"     — never answered (failed shards)
  "ungraded"     — answered, but nothing asked had an answer key
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


@pytest.fixture()
def aggregate():
    from integrations.agent_engine.hive_benchmark_prover import HiveBenchmarkProver
    return HiveBenchmarkProver.__dict__['_aggregate_results']


def _shard(status, score=0.0, solved=0, total=10, node='n1', ungraded=False):
    return {'node_id': node, 'shard_index': 0, 'status': status,
            'score': score, 'problems_solved': solved, 'problems_total': total,
            'ungraded': ungraded, 'time_seconds': 1.0}


# ── the run must not be publishable ──────────────────────────────────

def test_ungraded_run_has_no_score(aggregate):
    """The exact production shape: shards answered, nothing was gradable."""
    out = aggregate(None, [_shard('ungraded', ungraded=True),
                           _shard('ungraded', ungraded=True, node='n2')])
    assert out['valid'] is False, (
        "an ungraded run must not be valid — `valid` is what gates publishing, "
        "and this is the flag that let 416 fake proofs out")
    assert out['score'] is None, "0.0 here claims the hive answered and was wrong"
    assert out['ungraded_shards'] == 2


def test_ungraded_reason_says_ungraded_not_failed(aggregate):
    """The operator reading the log must not be sent hunting a dead endpoint.

    The pre-existing failure_reason for a scoreless run was 'no shard completed',
    which points at infrastructure. The cause here is the problem set, and the
    message has to say so or the next person debugs the wrong system.
    """
    out = aggregate(None, [_shard('ungraded', ungraded=True)])
    assert 'UNGRADED' in out['failure_reason']
    assert 'ground-truth' in out['failure_reason']
    assert 'not a hive score of zero' in out['failure_reason']


# ── it must not swallow the cases that ARE real ──────────────────────

def test_genuine_zero_still_survives(aggregate):
    """Answered every gradable question wrong is a legitimate 0.0. Keep it."""
    out = aggregate(None, [_shard('completed', score=0.0, solved=0, total=10)])
    assert out['valid'] is True
    assert out['score'] == 0.0
    assert out['ungraded_shards'] == 0


def test_ungraded_shard_does_not_dilute_a_graded_one(aggregate):
    """A shard with no answer key must not drag down a shard that was scored."""
    out = aggregate(None, [
        _shard('completed', score=0.9, solved=9, total=10),
        _shard('ungraded', ungraded=True, node='n2'),
    ])
    assert out['valid'] is True, "one real measurement is still a measurement"
    assert out['score'] == pytest.approx(0.9)
    assert out['ungraded_shards'] == 1
    assert out['num_nodes'] == 2, "per-node visibility of the ungraded shard is kept"


def test_failed_and_ungraded_are_distinguishable(aggregate):
    """Both suppress the score, for different reasons, and both stay visible."""
    out = aggregate(None, [_shard('failed'), _shard('ungraded', ungraded=True, node='n2')])
    assert out['valid'] is False
    statuses = sorted(n['status'] for n in out['per_node'])
    assert statuses == ['failed', 'ungraded']


# ── the duplicate-question guard ─────────────────────────────────────

class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _FakeDB:
    def __init__(self, result):
        self._result = result

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._result)


class _BrokenDB:
    def query(self, *_args, **_kwargs):
        raise RuntimeError('session is closed')


@pytest.fixture()
def open_exists():
    pytest.importorskip('integrations.social.models')
    from integrations.agent_engine.hive_benchmark_prover import _open_experiment_exists
    return _open_experiment_exists


def test_no_open_duplicate_allows_creation(open_exists):
    assert open_exists(_FakeDB(None), 'Benchmark priority: focus on ensemble_mmlu?') is False


def test_open_duplicate_blocks_creation(open_exists):
    assert open_exists(_FakeDB(object()), 'Benchmark priority: focus on ensemble_mmlu?') is True


def test_broken_check_fails_closed(open_exists):
    """If we cannot tell whether a duplicate exists, do not create another.

    Fail-open here would silently restore the runaway: the whole reason this
    guard exists is that 832 duplicates accumulated unnoticed. A gap in
    experiment creation is visible and recoverable; another 800 rows is not.
    """
    assert open_exists(_BrokenDB(), 'anything') is True
