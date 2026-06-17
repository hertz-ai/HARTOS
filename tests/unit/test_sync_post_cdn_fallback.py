"""#149 (C4): CDN retrieval fallback — pull from the source peer, fall back to
the durable copy at the parent tier (central/regional) when the peer yields
nothing (offline or empty).

federation.pull_with_central_fallback reuses pull_from_peer for BOTH legs (no
parallel fetch) + SyncEngine.parent_tier_url (the SAME resolver the sync drain
uses). Behavioral: monkeypatch the per-URL pull leg + the parent env, assert
which URLs get pulled.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.social.federation import federation  # noqa: E402


def _record(monkeypatch, per_url):
    """Patch pull_from_peer to record call order + return per-URL counts."""
    calls = []

    def fake(db, url, limit=20):
        calls.append(url)
        return per_url.get(url, 0)

    monkeypatch.setattr(federation, 'pull_from_peer', fake)
    return calls


def test_falls_back_to_central_when_peer_yields_nothing(monkeypatch):
    calls = _record(monkeypatch, {'http://central': 3})  # peer 0, central 3
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    out = federation.pull_with_central_fallback('DB', 'http://peerA', limit=20)
    assert out == 3
    assert calls == ['http://peerA', 'http://central']  # peer first, then origin


def test_no_fallback_when_peer_has_content(monkeypatch):
    calls = _record(monkeypatch, {'http://peerA': 5})
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    out = federation.pull_with_central_fallback('DB', 'http://peerA')
    assert out == 5
    assert calls == ['http://peerA']  # central never touched


def test_no_self_pull_when_peer_is_the_parent(monkeypatch):
    calls = _record(monkeypatch, {})  # everything 0
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    out = federation.pull_with_central_fallback('DB', 'http://central/')
    assert out == 0
    assert calls == ['http://central/']  # pulled once, no recursion


def test_regional_used_when_no_central(monkeypatch):
    calls = _record(monkeypatch, {'http://regional': 2})
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    monkeypatch.setenv('HEVOLVE_REGIONAL_URL', 'http://regional')
    out = federation.pull_with_central_fallback('DB', 'http://peerA')
    assert out == 2
    assert calls == ['http://peerA', 'http://regional']


def test_no_fallback_when_flat_node_has_no_parent(monkeypatch):
    calls = _record(monkeypatch, {})
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    monkeypatch.delenv('HEVOLVE_REGIONAL_URL', raising=False)
    out = federation.pull_with_central_fallback('DB', 'http://peerA')
    assert out == 0
    assert calls == ['http://peerA']  # flat node: peer only, no parent to fall back to
