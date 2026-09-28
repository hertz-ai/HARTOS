"""A peer row is 'active' only when its ADDRESS has been seen to reach this
node, and a loopback URL is admitted only from this machine.

Measured on the owner's desktop DB (2026-09-26, snapshot 08:43Z):

- 7,096 of 8,749 integrity challenges ended 'timeout' (81%): 4,635 against
  1,967 rows in 10.1.x, 2,015 against 898 loopback rows, 433 against
  192.168.x rows off this desktop's 192.168.0.x LAN.
- Every 10.1.x row first seen since 2026-09-25 (168) is KEYLESS, i.e. a
  relayed hint (_merge_peer plants a key only from a node's own signed
  announce), and each carries metadata observed_url http://172.21.0.1, the
  docker gateway on central (node 230c3115 at 192.168.0.9, whose own
  /api/social/peers shows 172.21.0.1 for every peer).  So these rows reach
  the desktop through central's peer list, and central itself cannot see
  where any announcer came from.
- The first sightings cluster in CI windows: 89% of 676 10.x rows and 17 of
  19 192.168.64.x rows fall inside a HARTOS Release run (+20 min), against
  47% of random instants; the desktop's own-LAN rows: 0 of 20.  A Release
  run's pytest shard (run 36152836720) shows the test process announcing to
  the real seeds and "Mind merge: auto-federated with 17343ed4 at
  http://10.1.0.62:6777".
- A relayed hint was stored as status 'active', so the integrity round
  challenged it: that is where the timeouts came from.
- A relayed record also copied the RELAYER's metadata into the new row, so
  central's vantage (observed_url 172.21.0.1) became the desktop's.
- A challenge to http://localhost:6777 on this desktop is a refused connect
  (nothing listens there; the bundled app serves :5000), which takes 4.15 s
  on Windows and is filed as 'timeout' by create_challenge.

The rule, reachability instead of address class (owner: security must not
partition the hive; 10.1.x and 192.168.x are real LANs, see
test_peer_url_hygiene):

- a NEW row is 'active' when a direct announce came FROM the address it
  claims (or the vantage is unknown, as before); otherwise it is 'stale'
  until the health round's ping reaches it, which makes it 'active';
- a loopback URL is refused when relayed, or when the announce came from
  another machine; a co-located node announcing over loopback is admitted;
- a relayed record never carries the relayer's metadata into the new row.

Driven with the REAL gossip object, the REAL handle_announce /
_merge_peer_list / _health_check_round / _integrity_round and the real
SQLite store.  Only the network is simulated.
"""
import types
import uuid
from datetime import datetime, timedelta

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import integrations.social.integrity_service as isvc
import integrations.social.peer_discovery as pd
import security.node_integrity as ni
from integrations.social.models import Base, PeerNode, db_session, get_engine

THIS_LAN_IP = '192.168.0.165'


@pytest.fixture
def gossip(monkeypatch):
    Base.metadata.create_all(get_engine())
    g = pd.gossip
    monkeypatch.setattr(g, '_running', True)
    monkeypatch.setattr(g, '_auto_federate_peer', lambda *a, **k: None)
    monkeypatch.setattr(g, '_integrity_cursor', 0, raising=False)
    monkeypatch.setattr(g, '_health_cursor', 0, raising=False)
    import core.port_registry as pr
    monkeypatch.setattr(pr, 'get_lan_ip', lambda: THIS_LAN_IP)
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(ni, '_private_key', priv)
    monkeypatch.setattr(ni, '_public_key', priv.public_key())
    yield g
    with db_session() as db:
        db.query(PeerNode).filter(PeerNode.node_id.like('addrtest-%')).delete(
            synchronize_session=False)


def _signed(url, **over):
    from security.hive_guardrails import get_guardrail_hash
    info = {'node_id': f'addrtest-{uuid.uuid4().hex[:10]}', 'url': url,
            'name': 'addrtest', 'version': '1.0.0',
            'public_key': ni.get_public_key_hex(),
            'guardrail_hash': get_guardrail_hash(),
            'timestamp': 1_900_000_000, 'tier': 'flat'}
    info.update(over)
    info['signature'] = ni.sign_json_payload(info)
    return info


def _hint(url, **over):
    """A record as central's /api/social/peers/exchange serves it: a
    PeerNode.to_dict(), keyless, carrying central's own metadata."""
    rec = {'node_id': f'addrtest-{uuid.uuid4().hex[:10]}', 'url': url,
           'name': 'hevolve-hint', 'version': '1.0.0', 'status': 'active',
           'public_key': None, 'tier': 'flat', 'capability_tier': 'standard',
           'metadata': {'observed_url': 'http://172.21.0.1:6777',
                        '_prev_agent_count': 0}}
    rec.update(over)
    return rec


def _row(node_id):
    with db_session() as db:
        r = db.query(PeerNode).filter_by(node_id=node_id).first()
        return None if r is None else types.SimpleNamespace(
            status=r.status, url=r.url, metadata=dict(r.metadata_json or {}))


# ── relayed hints ────────────────────────────────────────────────────────

def test_a_relayed_hint_is_not_active_until_its_address_answers(gossip):
    rec = _hint('http://10.1.0.179:6777')
    gossip._merge_peer_list([rec])
    assert _row(rec['node_id']).status == 'stale'


def test_a_relayed_hint_is_not_challenged(gossip, monkeypatch):
    rec = _hint('http://10.1.0.240:6777')
    gossip._merge_peer_list([rec])
    dialled = []

    def net(url, *a, **k):
        dialled.append(url)
        raise requests.ConnectionError('unreachable')
    monkeypatch.setattr(pd, 'pooled_get', net)
    monkeypatch.setattr(isvc, 'pooled_post', net)
    gossip._integrity_round()
    assert not [u for u in dialled if '10.1.0.240' in u], dialled


def test_a_relayed_hint_whose_address_answers_becomes_active(gossip,
                                                             monkeypatch):
    """Reachability decides, not the address class: a relayed 10.x peer on a
    network this node CAN reach (an Azure LAN, say) is promoted by the
    health round's ping, so no honest reachable peer is partitioned."""
    rec = _hint('http://10.1.0.83:6777')
    gossip._merge_peer_list([rec])

    def ping(url, timeout=None, **kw):
        if url.startswith('http://10.1.0.83:6777'):
            return types.SimpleNamespace(
                status_code=200, json=lambda: {'node_id': rec['node_id']})
        raise requests.ConnectionError('unreachable')
    monkeypatch.setattr(pd, 'pooled_get', ping)
    gossip._health_check_round()
    assert _row(rec['node_id']).status == 'active'


def test_a_relayed_hint_does_not_carry_the_relayers_metadata(gossip):
    rec = _hint('http://10.1.0.138:6777')
    gossip._merge_peer_list([rec])
    assert 'observed_url' not in _row(rec['node_id']).metadata


def test_a_relayed_loopback_hint_is_refused(gossip):
    """localhost in someone else's peer list names the relayer's machine,
    and from here it names this one: never the subject."""
    rec = _hint('http://localhost:6777')
    gossip._merge_peer_list([rec])
    assert _row(rec['node_id']) is None


# ── direct announces ─────────────────────────────────────────────────────

def test_a_direct_announce_from_the_address_it_claims_is_active(gossip):
    info = _signed('http://192.168.0.9:6777')
    reasons = []
    assert gossip.handle_announce(info, reasons=reasons,
                                  observed_ip='192.168.0.9') is True, reasons
    assert _row(info['node_id']).status == 'active'


def test_a_direct_announce_from_elsewhere_is_admitted_stale(gossip):
    """What central sees for every announcer: the claimed LAN address, from
    its docker gateway.  Admitted (never refused: the address may be real),
    but not active until a ping reaches it."""
    info = _signed('http://10.1.0.5:6777')
    reasons = []
    assert gossip.handle_announce(info, reasons=reasons,
                                  observed_ip='172.21.0.1') is True, reasons
    assert _row(info['node_id']).status == 'stale'


def test_a_direct_announce_with_no_vantage_is_active_as_before(gossip):
    info = _signed('http://192.168.0.9:6777')
    assert gossip.handle_announce(info) is True
    assert _row(info['node_id']).status == 'active'


def test_a_loopback_url_announced_from_another_machine_is_refused(gossip):
    info = _signed('http://localhost:6777')
    reasons = []
    assert gossip.handle_announce(info, reasons=reasons,
                                  observed_ip='192.168.0.9') is False
    assert _row(info['node_id']) is None
    assert reasons and 'loopback' in reasons[0], reasons


@pytest.mark.parametrize('source', ['127.0.0.1', '::1', THIS_LAN_IP])
def test_a_colocated_node_on_loopback_is_admitted(gossip, source):
    """Two nodes on one machine reach each other over loopback on distinct
    ports (tests/standalone/two_node_collaboration.py): an announce from this
    machine, over loopback or this host's own LAN address, is admitted."""
    info = _signed('http://127.0.0.1:7802')
    reasons = []
    assert gossip.handle_announce(info, reasons=reasons,
                                  observed_ip=source) is True, reasons
    assert _row(info['node_id']).status == 'active'


def test_a_dead_row_revived_from_elsewhere_is_stale_not_active(gossip):
    info = _signed('http://10.1.0.7:6777')
    gossip.handle_announce(info, observed_ip='10.1.0.7')
    with db_session() as db:
        r = db.query(PeerNode).filter_by(node_id=info['node_id']).one()
        r.status = 'dead'
        r.last_seen = datetime.utcnow() - timedelta(days=2)
    again = _signed('http://10.1.0.7:6777', node_id=info['node_id'])
    gossip.handle_announce(again, observed_ip='172.21.0.1')
    assert _row(info['node_id']).status == 'stale'
    again = _signed('http://10.1.0.7:6777', node_id=info['node_id'],
                    timestamp=1_900_000_001)
    gossip.handle_announce(again, observed_ip='10.1.0.7')
    assert _row(info['node_id']).status == 'active'


# ── the LAN beacon carries its measured source ──────────────────────────

def test_a_beacon_is_judged_by_the_address_it_came_from(gossip, monkeypatch):
    seen = {}

    def handle(payload, reasons=None, observed_ip=''):
        seen['observed_ip'] = observed_ip
        return True
    monkeypatch.setattr(gossip, 'handle_announce', handle)
    monkeypatch.setattr(gossip, '_announce_to_peer', lambda url: True)
    disco = pd.AutoDiscovery(gossip, port=1)
    payload = {'type': 'hevolve-discovery', 'node_id': 'addrtest-beacon',
               'url': 'http://192.168.0.9:6777'}
    monkeypatch.setattr(disco, '_parse_beacon', lambda data: dict(payload))

    class _Sock:
        calls = 0

        def recvfrom(self, n):
            _Sock.calls += 1
            if _Sock.calls > 1:
                disco._running = False
                raise OSError('closed')
            return b'x', ('192.168.0.9', 6780)
    disco._sock = _Sock()
    disco._running = True
    disco._recv_loop()
    assert seen['observed_ip'] == '192.168.0.9'
