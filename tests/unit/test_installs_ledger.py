"""Cumulative genuine-install ledger on FederatedAggregator.

/hive's nodes_reporting counts nodes active in the freshness window; it decays
and can never express a growing cumulative total. total_installs is the honest
monotonic count of distinct genuine node_ids that ever passed the join gate —
persistent, dedup'd, and gate-only so it cannot be inflated by unverified
traffic. This pins that behaviour.
"""
import json
import pytest

from integrations.agent_engine.federated_aggregator import FederatedAggregator


def test_record_dedup_and_count(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_AGENT_DATA', str(tmp_path))
    agg = FederatedAggregator()
    assert agg.total_genuine_installs() == 0
    agg._record_genuine_install('node-a')
    agg._record_genuine_install('node-b')
    agg._record_genuine_install('node-a')      # duplicate — must not double-count
    assert agg.total_genuine_installs() == 2


def test_empty_or_none_node_id_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_AGENT_DATA', str(tmp_path))
    agg = FederatedAggregator()
    agg._record_genuine_install('')
    agg._record_genuine_install(None)
    assert agg.total_genuine_installs() == 0


def test_persists_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_AGENT_DATA', str(tmp_path))
    FederatedAggregator()._record_genuine_install('persisted-node')
    # A fresh instance (e.g. after a container restart) reads the ledger back.
    assert FederatedAggregator().total_genuine_installs() == 1
    # And the on-disk form is the documented shape.
    data = json.loads((tmp_path / 'federation_installs.json').read_text())
    assert data['count'] == 1
    assert data['node_ids'] == ['persisted-node']


def test_census_exposes_total_installs(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_AGENT_DATA', str(tmp_path))
    agg = FederatedAggregator()
    agg._record_genuine_install('n1')
    agg._record_genuine_install('n2')
    census = agg.hive_census()
    assert census['total_installs'] == 2
    # nodes_reporting (active-in-window) is SEPARATE from the cumulative total.
    assert 'nodes_reporting' in census


def test_corrupt_ledger_is_non_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_AGENT_DATA', str(tmp_path))
    (tmp_path / 'federation_installs.json').write_text('{ not valid json')
    # Load must not raise; it degrades to an empty ledger.
    agg = FederatedAggregator()
    assert agg.total_genuine_installs() == 0
    agg._record_genuine_install('recovers')
    assert agg.total_genuine_installs() == 1
