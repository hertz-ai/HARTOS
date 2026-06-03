"""AutoDiscovery._parse_beacon must reject untrusted/malformed UDP beacons
without crashing the recv loop.

Regression: a valid-JSON but NON-dict payload (e.g. b'[1,2,3]') sailed past the
`except (ValueError, UnicodeDecodeError)` guard, then `payload.get('type')`
raised AttributeError on the list/str — escaping into _recv_loop. A remote peer
could crash the discovery thread with one crafted UDP packet.

Behavioural: real method over real bytes (instance built without __init__ so no
sockets are opened). No grep tests.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _disco():
    from integrations.social.peer_discovery import AutoDiscovery
    d = object.__new__(AutoDiscovery)   # skip __init__ → no UDP socket
    class _G:
        node_id = 'self-node'
    d._gossip = _G()
    return d


def test_non_dict_json_beacon_returns_empty_not_crash():
    d = _disco()
    magic = d.BEACON_MAGIC
    # Each is VALID json but not a dict — the pre-fix crash set.
    for body in (b'[1,2,3]', b'"just a string"', b'42', b'true', b'null'):
        assert d._parse_beacon(magic + body) == {}


def test_garbage_and_empty_and_non_magic_return_empty():
    d = _disco()
    assert d._parse_beacon(b'not-a-beacon-at-all') == {}      # no magic prefix
    assert d._parse_beacon(d.BEACON_MAGIC + b'{not valid json') == {}
    assert d._parse_beacon(d.BEACON_MAGIC + b'') == {}        # empty body


def test_wrong_type_dict_returns_empty():
    d = _disco()
    body = json.dumps({'type': 'something-else', 'node_id': 'n1'}).encode('utf-8')
    assert d._parse_beacon(d.BEACON_MAGIC + body) == {}


def test_self_beacon_is_ignored():
    """A dict beacon echoing our own node_id is dropped (don't discover self)."""
    d = _disco()
    body = json.dumps(
        {'type': 'hevolve-discovery', 'node_id': 'self-node'}).encode('utf-8')
    assert d._parse_beacon(d.BEACON_MAGIC + body) == {}
