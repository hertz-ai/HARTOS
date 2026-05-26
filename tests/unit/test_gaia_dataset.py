"""Unit tests for gaia_dataset loader — cache → HF → fallback."""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import gaia_dataset


def test_load_returns_empty_when_nothing_available(monkeypatch, tmp_path):
    # Point cache env at non-existent file; block HF import.
    monkeypatch.setenv(gaia_dataset._CACHE_PATH_ENV,
                       str(tmp_path / 'nope.json'))

    def _no_hf(*a, **kw):
        return []

    monkeypatch.setattr(gaia_dataset, '_try_huggingface', _no_hf)
    result = gaia_dataset.load_gaia_problems()
    assert result == []


def test_load_reads_cache_when_present(monkeypatch, tmp_path):
    cache = tmp_path / 'cache.json'
    problems = [
        {'level': 1, 'id': 'g1', 'prompt': 'Q1'},
        {'level': 2, 'id': 'g2', 'prompt': 'Q2'},
        {'level': 3, 'id': 'g3', 'prompt': 'Q3'},
    ]
    cache.write_text(json.dumps({'problems': problems}), encoding='utf-8')
    monkeypatch.setenv(gaia_dataset._CACHE_PATH_ENV, str(cache))

    got = gaia_dataset.load_gaia_problems(levels=[1, 3], limit=10)
    assert len(got) == 2
    assert {p['id'] for p in got} == {'g1', 'g3'}


def test_load_respects_limit(monkeypatch, tmp_path):
    cache = tmp_path / 'cache.json'
    problems = [
        {'level': 1, 'id': f'g{i}', 'prompt': f'Q{i}'}
        for i in range(20)
    ]
    cache.write_text(json.dumps({'problems': problems}), encoding='utf-8')
    monkeypatch.setenv(gaia_dataset._CACHE_PATH_ENV, str(cache))

    got = gaia_dataset.load_gaia_problems(levels=[1], limit=5)
    assert len(got) == 5


def test_save_cache_roundtrip(monkeypatch, tmp_path):
    cache = tmp_path / 'roundtrip.json'
    monkeypatch.setenv(gaia_dataset._CACHE_PATH_ENV, str(cache))
    original = [{'level': 1, 'id': 'x', 'prompt': 'hello'}]
    assert gaia_dataset.save_cache(original) is True
    got = gaia_dataset.load_gaia_problems()
    assert got == original


def test_fetch_problems_in_prover_uses_gaia_loader(monkeypatch, tmp_path):
    """hive_benchmark_prover._fetch_problems('gaia_mini') should route
    through gaia_dataset.load_gaia_problems.  When the loader returns
    empty, synthetic stubs take over."""
    from integrations.agent_engine import hive_benchmark_prover

    calls = {'n': 0}

    def _fake_loader(levels=None, limit=30):
        calls['n'] += 1
        # Return two problems to verify they flow through.
        return [
            {'level': 1, 'prompt': 'real-1'},
            {'level': 2, 'prompt': 'real-2'},
        ]

    monkeypatch.setattr(
        'integrations.agent_engine.gaia_dataset.load_gaia_problems',
        _fake_loader,
    )
    # Grab the prover singleton without side effects
    prover = hive_benchmark_prover.HiveBenchmarkProver()
    problems = prover._fetch_problems('gaia_mini', {})
    assert calls['n'] == 1
    assert len(problems) == 2
    assert all(p['type'] == 'agent' for p in problems)
    assert all(p['id'].startswith('gaia_mini_agent_') for p in problems)


def test_fetch_problems_falls_back_to_synthetic(monkeypatch):
    """When load_gaia_problems returns empty, synthetic stubs must be
    produced — never an empty list (the benchmark should always run)."""
    from integrations.agent_engine import hive_benchmark_prover

    monkeypatch.setattr(
        'integrations.agent_engine.gaia_dataset.load_gaia_problems',
        lambda levels=None, limit=30: [],
    )
    prover = hive_benchmark_prover.HiveBenchmarkProver()
    problems = prover._fetch_problems('gaia_mini', {})
    assert len(problems) > 0
    assert all(p['type'] == 'agent' for p in problems)
    # Must cover all levels per the spec
    levels = {p.get('level') for p in problems}
    assert 1 in levels and 2 in levels and 3 in levels


def test_gaia_in_known_baselines():
    """GAIA public scores must be present in KNOWN_BASELINES."""
    from integrations.agent_engine.hive_benchmark_prover import (
        KNOWN_BASELINES,
    )
    # At least 3 models carry a gaia_mini baseline
    gaia_models = [
        m for m, scores in KNOWN_BASELINES.items()
        if 'gaia_mini' in scores
    ]
    assert len(gaia_models) >= 3
    # GPT-4 + plugins is the canonical paper baseline — ~15%
    assert KNOWN_BASELINES['gpt-4-plugins']['gaia_mini'] == 0.15
