"""gossip.broadcast must be a bounded gossip fan-out, not a serial peer walk.

Live incident 2026-08-25 (desktop, installed build 13): resonance_tick →
gossip.broadcast walked hundreds of unreachable peer rows serially at
timeout=5 each, parking the federation_tick thread for 80+ minutes inside
one connect; the agent_daemon single-flight guard then blocked every
subsequent federation tick, so the node broadcast its learning delta exactly
once per boot.  py-spy stack: tick (federated_aggregator.py:241) →
_broadcast_resonance → GossipProtocol.broadcast (peer_discovery.py) →
urllib3 create_connection.

The fix mirrors b8523319 (which bounded broadcast_delta's own delivery but
left this shared primitive serial): filter class-unroutable rows, sample
gossip_fanout targets, deliver concurrently under one hard deadline.
"""

import threading
import time

import pytest
import requests

from integrations.social import peer_discovery as pd


def _fake_peers(n, prefix="10.99.0."):
    return [
        {"node_id": f"node-{i:03d}", "url": f"http://{prefix}{i % 250 + 1}:{6000 + i}"}
        for i in range(n)
    ]


@pytest.fixture
def gossip_instance(monkeypatch):
    g = pd.GossipProtocol()
    monkeypatch.setattr(g, "_is_peer_backed_off", lambda url: False)
    monkeypatch.setattr(g, "_record_peer_failure", lambda url: None)
    monkeypatch.setattr(g, "_record_peer_success", lambda url: None)
    g.gossip_fanout = 3
    return g


def test_broadcast_completes_within_deadline_not_serial(gossip_instance, monkeypatch):
    """40 slow peers x 0.5s each = 20s serial. Bounded delivery must finish well under that."""
    g = gossip_instance
    monkeypatch.setattr(g, "_load_peers_from_db", lambda exclude_dead=True: _fake_peers(40))

    calls = []

    def slow_dead_post(url, json=None, timeout=None):
        calls.append(url)
        time.sleep(0.5)
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(pd, "pooled_post", slow_dead_post)

    t0 = time.monotonic()
    sent = g.broadcast({"type": "test_msg"})
    elapsed = time.monotonic() - t0

    assert sent == 0
    assert elapsed < 8, (
        f"broadcast took {elapsed:.1f}s — serial walk; must be bounded fan-out"
    )


def test_broadcast_samples_fanout_not_everyone(gossip_instance, monkeypatch):
    """Without explicit targets, only ~gossip_fanout peers get the message per round."""
    g = gossip_instance
    monkeypatch.setattr(g, "_load_peers_from_db", lambda exclude_dead=True: _fake_peers(40))

    calls = []
    lock = threading.Lock()

    class _Resp:
        status_code = 200

    def counting_post(url, json=None, timeout=None):
        with lock:
            calls.append(url)
        return _Resp()

    monkeypatch.setattr(pd, "pooled_post", counting_post)

    sent = g.broadcast({"type": "test_msg"})

    assert len(calls) <= g.gossip_fanout, (
        f"{len(calls)} deliveries for fanout={g.gossip_fanout} — still broadcasting to everyone"
    )
    assert sent == len(calls)


def test_broadcast_skips_class_unroutable_rows(gossip_instance, monkeypatch):
    """docker-172.17 / dead-:677 rows are structurally unusable and must never be dialed."""
    g = gossip_instance
    peers = [
        {"node_id": "bad-1", "url": "http://172.17.0.4:6777"},
        {"node_id": "bad-2", "url": "http://10.1.1.5:677"},
        {"node_id": "good-1", "url": "http://192.168.7.7:6777"},
    ]
    monkeypatch.setattr(g, "_load_peers_from_db", lambda exclude_dead=True: peers)

    calls = []

    class _Resp:
        status_code = 200

    def counting_post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(pd, "pooled_post", counting_post)

    g.broadcast({"type": "test_msg"})

    assert all("172.17." not in u and ":677/" not in u for u in calls), calls
    assert any("192.168.7.7" in u for u in calls)


def test_broadcast_explicit_targets_are_all_delivered(gossip_instance, monkeypatch):
    """A directed broadcast (explicit targets) still reaches every named target."""
    g = gossip_instance
    peers = _fake_peers(10)
    monkeypatch.setattr(g, "_load_peers_from_db", lambda exclude_dead=True: peers)

    calls = []
    lock = threading.Lock()

    class _Resp:
        status_code = 200

    def counting_post(url, json=None, timeout=None):
        with lock:
            calls.append(url)
        return _Resp()

    monkeypatch.setattr(pd, "pooled_post", counting_post)

    wanted = ["node-002", "node-005", "node-007", "node-008", "node-009"]
    sent = g.broadcast({"type": "test_msg"}, targets=wanted)

    assert sent == len(wanted)
    assert len(calls) == len(wanted)
