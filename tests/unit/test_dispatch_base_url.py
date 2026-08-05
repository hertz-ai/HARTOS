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


# ── peer_discovery: the third caller of this same resolver ──
#
# dispatch and flask_integration were canonicalised onto get_local_backend_url
# (see the module docstring above). peer_discovery was missed, and kept its own
# `http://localhost:{get_port("backend")}`. That is why every peer in the live
# table advertises :6777 even on bundled installs where nothing listens there,
# and why NAT traversal (core/peer_link/nat.py), which resolves a peer from
# peer_info['url'], has only loopback addresses to work with.


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
    """No parallel 6777-hardcoding path left in discovery."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.setattr(pr, 'get_local_backend_url', lambda: 'http://sentinel:1234')
    assert g.base_url == 'http://sentinel:1234'


def test_peer_discovery_advertises_flask_port_when_bundled(monkeypatch):
    """The regression this exists for.

    Bundled desktop serves HARTOS in-process on the flask port; backend (6777)
    is dead. Discovery must advertise what is actually listening, because that
    string is what every peer stores and what NAT traversal dials.
    """
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert g.base_url == f'http://localhost:{flask_port}'
    assert str(pr.get_port('backend')) not in g.base_url


def test_peer_discovery_base_url_is_not_frozen_at_import(monkeypatch):
    """Resolution must be lazy, not computed in __init__.

    `gossip = GossipProtocol()` runs at import time, before any server is
    listening. Resolving then would always take the resolver's cold-boot
    fallback and re-introduce the dead :6777 permanently. Simulate booting:
    nothing listening at first read, flask up at the next.
    """
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)

    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: False)
    cold = g.base_url
    assert cold == f'http://localhost:{pr.get_port("backend")}'

    # Server finishes starting. Expire the short cache the way time would.
    g._base_url_cache_ts = 0.0
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert g.base_url == f'http://localhost:{flask_port}'


def test_peer_discovery_honours_env_override(monkeypatch):
    """HEVOLVE_BASE_URL is how a remote/cloud node advertises a routable
    address today; the shared resolver checks it first, so discovery inherits
    that for free."""
    import core.port_registry as pr
    pd, g = _fresh_gossip()
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'https://my-node.example.com:6777')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: True)
    assert g.base_url == 'https://my-node.example.com:6777'
