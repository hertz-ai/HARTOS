"""Peer-discovery first-beacon dedup must be BOUNDED (#83).

`AutoDiscovery._discovered_nodes` suppresses re-logging + re-gossiping a node
on every ~30s beacon.  It used to be a plain `set` that only ever grew — on a
long-running node in a churny LAN (peers cycling through fresh node_ids) it
leaked memory without bound.  It is now a size-capped, TTL-expiring TTLCache.

These tests construct a REAL AutoDiscovery (mock gossip — no sockets are opened
in __init__) and drive node_ids through the dedup exactly as the recv loop does
(`in` check + item-set), asserting it stays bounded, still dedups, and expires.
No grep tests.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.peer_discovery import AutoDiscovery


def _disc(ttl=60, max_size=5):
    # __init__ reads these envs when it builds the dedup cache.
    os.environ['HEVOLVE_DISCOVERY_DEDUP_TTL'] = str(ttl)
    os.environ['HEVOLVE_DISCOVERY_DEDUP_MAX'] = str(max_size)
    return AutoDiscovery(MagicMock())


def test_dedup_is_bounded_not_an_unbounded_set():
    d = _disc(ttl=60, max_size=5)
    # The leak was a plain set; it must no longer be one.
    assert not isinstance(d._discovered_nodes, set)
    # Push far more than the cap, the way the recv loop does.
    for i in range(200):
        d._discovered_nodes[f'node-{i}'] = True
    assert len(d._discovered_nodes) <= 5   # bounded — the unbounded-growth leak is fixed


def test_membership_dedups_a_repeat_beacon():
    d = _disc()
    assert 'n1' not in d._discovered_nodes      # first sighting -> would be processed
    d._discovered_nodes['n1'] = True
    assert 'n1' in d._discovered_nodes          # repeat beacon -> suppressed


def test_entry_expires_after_ttl_so_node_is_rediscoverable():
    d = _disc(ttl=1, max_size=5)
    d._discovered_nodes['n1'] = True
    assert 'n1' in d._discovered_nodes
    time.sleep(1.2)                             # past the 1s TTL
    assert 'n1' not in d._discovered_nodes      # expired -> the node can be re-discovered


def test_recently_seen_nodes_survive_eviction_of_oldest():
    # FIFO eviction drops the OLDEST, not the newest — a node still actively
    # beaconing should not be evicted while older silent ones are present.
    d = _disc(ttl=60, max_size=3)
    for nid in ('a', 'b', 'c'):
        d._discovered_nodes[nid] = True
    d._discovered_nodes['d'] = True             # evicts 'a' (oldest)
    assert 'a' not in d._discovered_nodes
    assert 'd' in d._discovered_nodes and 'c' in d._discovered_nodes
