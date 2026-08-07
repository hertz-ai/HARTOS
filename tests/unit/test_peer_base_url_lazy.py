"""The peer-advertised base_url must track the port that is actually LISTENING.

LIVE BUG (measured 2026-08-03 against the bundled desktop):
    GET /api/social/peers -> peers[0].url == "http://localhost:6777"
    but Nunba serves on :5000, and :6777 refused the connection (curl http=000).
Every peer that discovered this node was handed a dead URL.

Two causes, both fixed here:

1. peer_discovery.py:150 built the URL itself —
       f'http://localhost:{get_port("backend")}'
   — instead of calling core.port_registry.get_local_backend_url(), whose own
   docstring exists for exactly this: "ONE resolver, so neither hardcodes a
   dead :6777 in bundled mode". Four other call sites already use it; peer
   discovery, where a wrong URL is most costly, was the holdout.

2. Resolving it in __init__ cannot work either. `gossip = GossipProtocol()`
   runs at MODULE SCOPE, i.e. at import time, before Flask binds. Any value
   computed there is the cold-boot fallback, frozen for the process lifetime.
   Hence the lazy property.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

peer_discovery = pytest.importorskip("integrations.social.peer_discovery")
port_registry = pytest.importorskip("core.port_registry")

GossipProtocol = peer_discovery.GossipProtocol


def _bare_gossip():
    """A GossipProtocol without running __init__ (which touches DB, keys, net).

    Only the two fields the property owns are initialised, so the property
    under test runs for real.
    """
    g = object.__new__(GossipProtocol)
    g._base_url_cached = ''
    g._base_url_final = False
    return g


# The LAN address these tests DECLARE (TEST-NET-1, never a real host). The
# resolver's contract moved with e91600d3: base_url is now the ADVERTISABLE
# form — get_lan_ip() paired with the live port — because publishing loopback
# made every peer dial itself (the live peer table read http://localhost:6777
# on every row). These tests asserted the loopback form, so they pinned the
# bug; worse, they left get_lan_ip unmocked, so they read the DEV BOX'S REAL
# NIC ('http://192.168.0.15:...' in the failure output) — the machine-identity
# leak again. The LAN address is declared here and the expectations follow the
# advertised contract.
LAN = "192.0.2.7"


@pytest.fixture(autouse=True)
def _sealed_machine(monkeypatch):
    monkeypatch.delenv("HEVOLVE_BASE_URL", raising=False)
    monkeypatch.setattr(port_registry, "get_lan_ip", lambda: LAN)


def test_does_not_freeze_the_cold_boot_fallback(monkeypatch):
    """THE regression: constructed before Flask binds, read after.

    Nothing is listening at construction time; the resolver returns its backend
    fallback. Later Flask comes up on 5000. The property must report 5000 —
    the old code reported the fallback forever.
    """
    backend = port_registry.get_port("backend")
    flask = port_registry.get_port("flask")
    listening: set[int] = set()
    monkeypatch.setattr(
        port_registry, "_is_port_listening",
        lambda port, *a, **kw: int(port) in listening,
    )

    g = _bare_gossip()  # "constructed" while nothing listens
    assert g.base_url == f"http://{LAN}:{backend}", "expected the fallback"

    listening.add(flask)  # Flask finishes binding
    assert g.base_url == f"http://{LAN}:{flask}", (
        "base_url froze the cold-boot fallback — every peer would dial a dead "
        "port for the life of the process"
    )


def test_no_lan_address_degrades_to_the_local_url(monkeypatch):
    """Precedence rung 3: with NO usable LAN address (get_lan_ip ''), the
    advertised URL falls back to the local form — no worse than before the
    advertisable resolver existed."""
    flask = port_registry.get_port("flask")
    monkeypatch.setattr(port_registry, "get_lan_ip", lambda: "")
    monkeypatch.setattr(
        port_registry, "_is_port_listening",
        lambda port, *a, **kw: int(port) == flask,
    )
    g = _bare_gossip()
    assert g.base_url == f"http://localhost:{flask}"


def test_a_real_answer_is_cached(monkeypatch):
    flask = port_registry.get_port("flask")
    calls = []

    def counting(port, *a, **kw):
        calls.append(int(port))
        return int(port) == flask

    monkeypatch.setattr(port_registry, "_is_port_listening", counting)

    g = _bare_gossip()
    assert g.base_url == f"http://{LAN}:{flask}"
    n = len(calls)
    for _ in range(5):
        g.base_url
    assert len(calls) == n, (
        "re-probes after a definitive answer; base_url is read once per gossip "
        "round and each probe is a TCP connect"
    )


def test_env_override_wins_and_is_final(monkeypatch):
    monkeypatch.setenv("HEVOLVE_BASE_URL", "http://node.example:9999")
    calls = []
    monkeypatch.setattr(
        port_registry, "_is_port_listening",
        lambda port, *a, **kw: calls.append(int(port)) or False,
    )
    g = _bare_gossip()
    assert g.base_url == "http://node.example:9999"
    g.base_url
    assert not calls, "env override must not probe local ports at all"


def test_no_trailing_slash(monkeypatch):
    monkeypatch.setenv("HEVOLVE_BASE_URL", "http://node.example:9999/")
    g = _bare_gossip()
    assert g.base_url == "http://node.example:9999", (
        "peers concatenate paths onto this; a trailing slash yields '//api/...'"
    )


def test_self_info_publishes_the_resolved_url(monkeypatch):
    """_self_info() is what actually reaches other nodes."""
    flask = port_registry.get_port("flask")
    monkeypatch.setattr(
        port_registry, "_is_port_listening",
        lambda port, *a, **kw: int(port) == flask,
    )
    g = _bare_gossip()
    g.node_id = "n1"
    g.node_name = "test"
    g.version = "1.0.0"
    g.tier = "flat"
    g._hart_tag = None
    g._get_count = lambda _what: 0

    info = g._self_info()
    assert info["url"] == f"http://{LAN}:{flask}", (
        "the advertised url is what peers dial; it must be the LAN address "
        "with the live port — loopback here made every peer dial itself"
    )


def test_peer_discovery_uses_the_canonical_resolver():
    """Drift-guard: no second way to compute this node's base URL.

    core/port_registry.get_local_backend_url is the single source (Gate 4).
    Re-deriving it from get_port('backend') is what shipped the dead :6777.
    """
    src = Path(peer_discovery.__file__).read_text(encoding="utf-8", errors="replace")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "get_local_backend_url" in code, (
        "peer_discovery no longer delegates to the canonical resolver"
    )
    head = code.split("def __init__", 1)[-1][:2000]
    assert 'get_port("backend")' not in head and "get_port('backend')" not in head, (
        "__init__ re-derives the base URL from get_port('backend') again — that "
        "hardcodes :6777 and is dead in the bundled desktop"
    )
