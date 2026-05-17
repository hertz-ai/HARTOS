"""Unit tests for HiveConsensus 4-of-4 vote gate.

The brief §3.4 + §5-C mandates the 4-of-4 democratic consensus before
ANY agent upgrade lands.  These tests verify every vote source and
every combination of pass/fail → final approval.
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

# Ensure repo root is on sys.path for direct pytest invocation
_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import hive_consensus, reasoning_trace


def _fresh_trace_file(tmp_path, monkeypatch):
    """Point reasoning_trace at tmp_path so tests don't touch prod data."""
    trace_dir = tmp_path / 'reasoning_traces'
    monkeypatch.setattr(
        reasoning_trace,
        '_resolve_trace_dir',
        lambda: str(trace_dir),
    )
    return trace_dir


def _stub_vote_ok(cls_method_name: str):
    """Patch a Vote-returning classmethod to always return pass."""
    return mock.patch.object(
        hive_consensus.HiveConsensus,
        cls_method_name,
        classmethod(lambda cls, *a, **kw: hive_consensus.Vote(
            name=cls_method_name, passed=True, reason='stub',
        )),
    )


def _stub_vote_fail(cls_method_name: str, reason: str = 'stub-fail'):
    return mock.patch.object(
        hive_consensus.HiveConsensus,
        cls_method_name,
        classmethod(lambda cls, *a, **kw: hive_consensus.Vote(
            name=cls_method_name, passed=False, reason=reason,
        )),
    )


# ───── Circuit breaker vote ─────

def test_circuit_breaker_vote_pass_when_not_halted(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)
    with mock.patch(
        'security.hive_guardrails.HiveCircuitBreaker.is_halted',
        return_value=False,
    ):
        v = hive_consensus.HiveConsensus._vote_circuit_breaker()
    assert v.passed is True
    assert v.name == 'circuit_breaker'


def test_circuit_breaker_vote_fail_when_halted(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)
    with mock.patch(
        'security.hive_guardrails.HiveCircuitBreaker.is_halted',
        return_value=True,
    ), mock.patch(
        'security.hive_guardrails.HiveCircuitBreaker.get_status',
        return_value={'halted': True, 'reason': 'test'},
    ):
        v = hive_consensus.HiveConsensus._vote_circuit_breaker()
    assert v.passed is False
    assert 'halted' in v.reason.lower()


# ───── Constitutional vote ─────

def test_constitutional_vote_rejects_protected_files(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)
    v = hive_consensus.HiveConsensus._vote_constitutional(
        new_content='harmless prompt',
        target_files=['security/hive_guardrails.py'],
    )
    assert v.passed is False
    assert 'protected' in v.reason.lower() or 'hive_guardrails' in v.reason


def test_constitutional_vote_accepts_benign_prompt(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)
    v = hive_consensus.HiveConsensus._vote_constitutional(
        new_content='Help the user accomplish their stated goal clearly.',
        target_files=[],
    )
    assert v.passed is True


# ───── Local probe vote (reads _Leaderboard) ─────

def test_local_probe_vote_pass_when_leaderboard_beats_baseline(
    tmp_path, monkeypatch,
):
    _fresh_trace_file(tmp_path, monkeypatch)

    class _StubLB:
        def get_best_scores(self):
            return {'goal:marketing': {'score': 0.85, 'run_id': 'x'}}

        def compare_to_baselines(self):
            return {'goal:marketing': {'hive': 0.85, 'margin_vs_best': 0.05}}

    class _StubProver:
        _leaderboard = _StubLB()

    with mock.patch(
        'integrations.agent_engine.hive_benchmark_prover.get_benchmark_prover',
        return_value=_StubProver(),
    ):
        v = hive_consensus.HiveConsensus._vote_local_probe(
            goal_type='marketing', probe_evidence=None,
        )
    assert v.passed is True, v.reason


def test_local_probe_vote_fail_when_margin_negative(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)

    class _StubLB:
        def get_best_scores(self):
            return {'goal:marketing': {'score': 0.4}}

        def compare_to_baselines(self):
            return {'goal:marketing': {'margin_vs_best': -0.1}}

    class _StubProver:
        _leaderboard = _StubLB()

    with mock.patch(
        'integrations.agent_engine.hive_benchmark_prover.get_benchmark_prover',
        return_value=_StubProver(),
    ):
        v = hive_consensus.HiveConsensus._vote_local_probe(
            goal_type='marketing', probe_evidence=None,
        )
    assert v.passed is False


def test_local_probe_vote_uses_explicit_evidence(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)
    v = hive_consensus.HiveConsensus._vote_local_probe(
        goal_type='coding',
        probe_evidence={'margin_vs_best': 0.02, 'score': 0.72},
    )
    assert v.passed is True


def test_local_probe_vote_accepts_no_baseline_when_score_high(
    tmp_path, monkeypatch,
):
    """A new goal_type has no public baseline — we accept a score >= 0.5."""
    _fresh_trace_file(tmp_path, monkeypatch)

    class _StubLB:
        def get_best_scores(self):
            return {'goal:speech_therapy': {'score': 0.7}}

        def compare_to_baselines(self):
            return {'goal:speech_therapy': {'hive': 0.7,
                                            'margin_vs_best': None}}

    class _StubProver:
        _leaderboard = _StubLB()

    with mock.patch(
        'integrations.agent_engine.hive_benchmark_prover.get_benchmark_prover',
        return_value=_StubProver(),
    ):
        v = hive_consensus.HiveConsensus._vote_local_probe(
            goal_type='speech_therapy', probe_evidence=None,
        )
    assert v.passed is True


# ───── Peer probe quorum vote ─────

def test_peer_quorum_pass_when_no_peers(tmp_path, monkeypatch):
    """Single-node deploys have no peers — the vote passes with a note."""
    _fresh_trace_file(tmp_path, monkeypatch)

    class _StubAgg:
        _lock = mock.MagicMock(
            __enter__=lambda self: None,
            __exit__=lambda *a: None,
        )
        _peer_deltas: dict = {}

    stub = _StubAgg()
    with mock.patch(
        'integrations.agent_engine.federated_aggregator.get_federated_aggregator',
        return_value=stub,
    ):
        v = hive_consensus.HiveConsensus._vote_peer_probe_quorum('marketing')
    assert v.passed is True


def test_peer_quorum_pass_when_three_peers_agree(tmp_path, monkeypatch):
    _fresh_trace_file(tmp_path, monkeypatch)

    class _StubAgg:
        def __init__(self):
            import threading
            self._lock = threading.Lock()
            self._peer_deltas = {
                'peer1': {'benchmark_results': {
                    'goal:marketing': {'value': 0.8, 'baseline': 0.6},
                }},
                'peer2': {'benchmark_results': {
                    'goal:marketing': {'value': 0.7, 'baseline': 0.5},
                }},
                'peer3': {'benchmark_results': {
                    'goal:marketing': {'value': 0.9, 'baseline': 0.55},
                }},
            }

    stub = _StubAgg()
    with mock.patch(
        'integrations.agent_engine.federated_aggregator.get_federated_aggregator',
        return_value=stub,
    ):
        v = hive_consensus.HiveConsensus._vote_peer_probe_quorum('marketing')
    assert v.passed is True
    assert '3 peers agree' in v.reason


# ───── Full upgrade_proposal integration ─────

def test_upgrade_proposal_rejects_when_any_vote_fails(tmp_path, monkeypatch):
    trace_dir = _fresh_trace_file(tmp_path, monkeypatch)
    with _stub_vote_ok('_vote_circuit_breaker'), \
         _stub_vote_ok('_vote_constitutional'), \
         _stub_vote_fail('_vote_local_probe', 'no leaderboard'), \
         _stub_vote_ok('_vote_peer_probe_quorum'):
        decision = hive_consensus.HiveConsensus.upgrade_proposal(
            prompt_id='atlas',
            goal_type='coding',
            new_content='New prompt — help users write code.',
        )
    assert decision.approved is False
    assert 'local_probe' in decision.reason
    # Reasoning trace must record the rejection
    trace_files = list(trace_dir.glob('*.jsonl'))
    assert trace_files, 'reasoning_trace should have written a decision'
    lines = trace_files[0].read_text(encoding='utf-8').strip().splitlines()
    assert lines, 'trace file empty'
    entry = json.loads(lines[-1])
    assert entry['approved'] is False
    assert entry['action'] == 'upgrade_proposal'


def test_upgrade_proposal_approves_when_all_four_pass(tmp_path, monkeypatch):
    trace_dir = _fresh_trace_file(tmp_path, monkeypatch)
    with _stub_vote_ok('_vote_circuit_breaker'), \
         _stub_vote_ok('_vote_constitutional'), \
         _stub_vote_ok('_vote_local_probe'), \
         _stub_vote_ok('_vote_peer_probe_quorum'):
        decision = hive_consensus.HiveConsensus.upgrade_proposal(
            prompt_id='sage',
            goal_type='learning',
            new_content='Coordinate learning goals across the hive.',
        )
    assert decision.approved is True
    assert decision.reason == 'all 4 votes passed'
    # Audit trail written
    trace_files = list(trace_dir.glob('*.jsonl'))
    assert trace_files
    entry = json.loads(
        trace_files[0].read_text(encoding='utf-8').strip().splitlines()[-1]
    )
    assert entry['approved'] is True
    assert len(entry['votes']) == 4
    assert all(v['passed'] for v in entry['votes'].values())


def test_upgrade_proposal_rejects_protected_file_edit(tmp_path, monkeypatch):
    """End-to-end: editing a PROTECTED_FILES entry cannot be approved."""
    _fresh_trace_file(tmp_path, monkeypatch)
    # Keep the other three votes passing; only constitutional will fail.
    with _stub_vote_ok('_vote_circuit_breaker'), \
         _stub_vote_ok('_vote_local_probe'), \
         _stub_vote_ok('_vote_peer_probe_quorum'):
        decision = hive_consensus.HiveConsensus.upgrade_proposal(
            prompt_id='scout',
            goal_type='ip_protection',
            new_content='Innocuous text.',
            target_files=['security/hive_guardrails.py'],
        )
    assert decision.approved is False
    assert any(
        not v.passed and v.name == 'constitutional'
        for v in decision.votes
    )
