"""Live self-info must win over any persisted self-row in /api/social/peers.

Task #596.  ``GossipProtocol.get_peer_list`` used to append ``_self_info()``
only when the PeerNode table did not already contain a row for our own
node_id::

    if not any(p.get('node_id') == self.node_id for p in peers):
        peers.append(self_info)

Once such a row exists that guard is False, the fresh ``_self_info()`` is
discarded, and the node reports stale data about itself.  Nothing culls the
row either — ``_load_peers_from_db`` filters on ``PeerNode.status != 'dead'``,
a status column rather than a ``last_seen`` age check.

Measured on a live node 2026-08-03: /api/social/peers returned its own node_id
with ``endpoint=None``, all capability/compute fields None, and
``last_seen="2026-04-30T07:16:29"`` — 95 days stale while the node was running
and its gossip ``_send_loop`` was provably alive (py-spy).

These tests drive the REAL method with the DB layer stubbed, so they verify
behaviour rather than re-implementing it.
"""
from __future__ import annotations

import pytest

from integrations.social.peer_discovery import GossipProtocol

SELF_ID = "46329c87-cbb6-4ca1-bad5-816f6007b6a0"
REMOTE_ID = "11111111-2222-3333-4444-555555555555"

# Shape produced by PeerNode.to_dict() — note 'endpoint'/'last_seen'.
STALE_SELF_ROW = {
    "node_id": SELF_ID,
    "endpoint": None,
    "last_seen": "2026-04-30T07:16:29",
    "capability_tier": None,
    "compute_gpu_count": None,
    "certificate_verified": False,
}
REMOTE_ROW = {
    "node_id": REMOTE_ID,
    "endpoint": "http://192.168.1.50:5000",
    "last_seen": "2026-08-03T05:39:00",
}
# Shape produced by _self_info() — note 'url'/'timestamp'.
LIVE_SELF = {
    "node_id": SELF_ID,
    "url": "http://192.168.1.10:5000",
    "name": "nunba-local",
    "timestamp": 1754200000,
    "agent_count": 7,
}


def _make_gossip(db_rows):
    """Real GossipProtocol with only the DB layer stubbed.

    __init__ opens sockets and touches the DB, so bypass it — these tests are
    about get_peer_list's selection logic, nothing else.
    """
    g = object.__new__(GossipProtocol)
    g.node_id = SELF_ID
    g._load_peers_from_db = lambda exclude_dead=True: [dict(r) for r in db_rows]
    g._self_info = lambda: dict(LIVE_SELF)
    return g


def test_stale_self_row_does_not_shadow_live_self_info():
    """The exact production failure: a persisted self-row must be ignored."""
    g = _make_gossip([STALE_SELF_ROW])
    peers = g.get_peer_list()

    selves = [p for p in peers if p.get("node_id") == SELF_ID]
    assert len(selves) == 1, f"expected exactly one self entry, got {len(selves)}"
    me = selves[0]
    assert me.get("last_seen") != "2026-04-30T07:16:29", (
        "self entry is still the stale PeerNode row (task #596)"
    )
    assert me.get("url") == LIVE_SELF["url"], (
        "self entry should be live _self_info(), not the persisted row"
    )
    assert me.get("agent_count") == 7


def test_self_is_present_even_with_no_db_rows():
    """Self inclusion is intentional — do not regress it into exclusion."""
    g = _make_gossip([])
    peers = g.get_peer_list()
    assert [p["node_id"] for p in peers] == [SELF_ID]


def test_remote_peers_are_preserved_alongside_self():
    g = _make_gossip([REMOTE_ROW, STALE_SELF_ROW])
    peers = g.get_peer_list()
    ids = sorted(p["node_id"] for p in peers)
    assert ids == sorted([REMOTE_ID, SELF_ID])
    remote = next(p for p in peers if p["node_id"] == REMOTE_ID)
    assert remote["endpoint"] == "http://192.168.1.50:5000", (
        "remote rows must pass through untouched"
    )


def test_no_duplicate_self_entry():
    """Appending unconditionally must not double-count self."""
    g = _make_gossip([STALE_SELF_ROW, dict(STALE_SELF_ROW)])
    peers = g.get_peer_list()
    assert sum(1 for p in peers if p["node_id"] == SELF_ID) == 1


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], 0),                              # fresh single node
        ([STALE_SELF_ROW], 0),                # self only — the live case
        ([REMOTE_ROW], 1),
        ([REMOTE_ROW, STALE_SELF_ROW], 1),
    ],
)
def test_count_remote_peers_never_counts_self(rows, expected):
    """len(get_peer_list()) is never 0, so it cannot answer "am I federated?".

    The live node reported count=1 on a machine with zero remote peers, and
    dashboards rendered that as a peer.
    """
    assert _make_gossip(rows).count_remote_peers() == expected


def test_count_remote_peers_disagrees_with_len_on_a_lone_node():
    """Pins WHY remote_count exists — otherwise someone 'simplifies' it away."""
    g = _make_gossip([STALE_SELF_ROW])
    assert len(g.get_peer_list()) == 1, "self is in the list by design"
    assert g.count_remote_peers() == 0, "but there are no REMOTE peers"
