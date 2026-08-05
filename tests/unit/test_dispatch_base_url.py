"""#71 + omni-channel inbound — the live local HARTOS backend URL resolver.

dispatch's Tier-2 fallback AND the channel inbound bridge (flask_integration)
used to hardcode their target to get_port('backend')=6777. In a BUNDLED desktop
HARTOS is served in-process on the Flask port (5000) and the standalone 6777
subprocess isn't running, so both hit a dead port. They now share ONE resolver,
core.port_registry.get_local_backend_url, which probes the live local port.

Behavioral: mock the port-probe boundary, call the real resolver, assert the URL.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def pr():
    import core.port_registry as m
    return m


def test_env_base_url_wins_verbatim(pr, monkeypatch):
    """HEVOLVE_BASE_URL (remote/cloud + shared discovery/federation base) is
    used verbatim, even if a local port would probe live."""
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://remote-host:6777')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: True)
    assert pr.get_local_backend_url() == 'http://remote-host:6777'


def test_bundled_picks_flask_when_backend_dead(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert pr.get_local_backend_url() == f'http://localhost:{flask_port}'


def test_standalone_picks_backend_when_live(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    backend_port = pr.get_port('backend')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == backend_port)
    assert pr.get_local_backend_url() == f'http://localhost:{backend_port}'


def test_coldboot_falls_back_to_backend(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: False)
    assert pr.get_local_backend_url() == f'http://localhost:{pr.get_port("backend")}'


def test_dispatch_delegates_to_canonical_resolver(monkeypatch):
    """dispatch._local_dispatch_base_url is now a thin delegate — no parallel
    6777-hardcoding path."""
    try:
        import core.port_registry as pr
        import integrations.agent_engine.dispatch as d
    except Exception as e:
        pytest.skip(f"dispatch unavailable: {e}")
    monkeypatch.setattr(pr, 'get_local_backend_url', lambda: 'http://sentinel:1234')
    assert d._local_dispatch_base_url() == 'http://sentinel:1234'


# ── peer_discovery: advertising, which is a different question ──
#
# dispatch and flask_integration were canonicalised onto get_local_backend_url
# (see the module docstring). peer_discovery was missed and kept its own
# `http://localhost:{get_port("backend")}`.
#
# But discovery needs a different answer from those two. They ask "where do I
# reach my OWN backend", and loopback is correct. Discovery PUBLISHES its
# answer to other machines, and loopback is never correct for that: it is why
# every peer in the live table advertised http://localhost:6777, and why
# core/peer_link/nat.py, which dials peer_info['url'], only ever resolved back
# to the caller's own machine.
#
# So discovery uses get_advertisable_base_url: same live-port probing, plus a
# host another node can actually use.


def _fresh_gossip():
    """A GossipProtocol instance with its base_url cache cleared.

    The module instantiates `gossip = GossipProtocol()` at import time, so the
    shared instance may already hold a resolved value.
    """
    try:
        import integrations.social.peer_discovery as pd
    except Exception as e:  # pragma: no cover - env without deps
        pytest.skip(f"peer_discovery unavailable: {e}")
    g = pd.gossip
    g._base_url_cache = ''
    g._base_url_cache_ts = 0.0
    return pd, g


def test_peer_discovery_delegates_to_canonical_resolver(monkeypatch):
    """No parallel address-building path left in discovery."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.setattr(pr, 'get_advertisable_base_url', lambda: 'http://sentinel:1234')
    assert g.base_url == 'http://sentinel:1234'


def test_peer_discovery_advertises_lan_host_not_loopback(monkeypatch):
    """The regression this exists for.

    A peer stores this string and NAT traversal dials it. Loopback sends every
    peer back to its own machine.
    """
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '192.168.1.50')
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert g.base_url == f'http://192.168.1.50:{flask_port}'
    assert 'localhost' not in g.base_url
    assert '127.' not in g.base_url


def test_peer_discovery_advertises_the_live_port_when_bundled(monkeypatch):
    """Bundled serves HARTOS in-process on flask; 6777 is dead there."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '10.0.0.4')
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert g.base_url.endswith(f':{flask_port}')
    assert str(pr.get_port('backend')) not in g.base_url


def test_peer_discovery_falls_back_to_local_when_no_lan_ip(monkeypatch):
    """No usable LAN address is no worse than today, not a crash."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: False)
    assert g.base_url == f'http://localhost:{pr.get_port("backend")}'


def test_peer_discovery_base_url_is_not_frozen_at_import(monkeypatch):
    """Resolution must be lazy, not computed in __init__.

    `gossip = GossipProtocol()` runs at import time, before any server is
    listening and often before the network is up. Resolving there would freeze
    the cold-boot fallback permanently.
    """
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)

    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: False)
    assert g.base_url == f'http://localhost:{pr.get_port("backend")}'

    # Network and server come up. Expire the short cache the way time would.
    g._base_url_cache_ts = 0.0
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '192.168.1.77')
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert g.base_url == f'http://192.168.1.77:{flask_port}'


def test_peer_discovery_honours_env_override(monkeypatch):
    """HEVOLVE_BASE_URL stays authoritative: it is how Docker Compose and
    remote SSH installs already publish a routable name."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'https://my-node.example.com:6777')
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '192.168.1.50')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: True)
    assert g.base_url == 'https://my-node.example.com:6777'


# ── the advertisable resolver itself ──

def test_advertisable_never_returns_loopback_when_lan_exists(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: '172.16.5.9')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: True)
    url = pr.get_advertisable_base_url()
    assert url.startswith('http://172.16.5.9:')
    assert 'localhost' not in url


def test_lan_ip_rejects_loopback(pr, monkeypatch):
    """A stack that answers 127.0.0.1 is useless to advertise, so treat it as
    no answer rather than publishing it."""
    class _S:
        def settimeout(self, *_a): pass
        def connect(self, *_a): pass
        def getsockname(self): return ('127.0.0.1', 9)
        def close(self): pass
    monkeypatch.setattr(pr.socket, 'socket', lambda *a, **k: _S())
    assert pr.get_lan_ip() == ''
