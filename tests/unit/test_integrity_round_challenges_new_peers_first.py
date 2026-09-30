"""A live peer that just announced itself is challenged in the NEXT integrity
round, not after the round has worked through the backlog.

Measured on the owner's desktop DB (2026-09-26): from first seen to first
passed challenge took 23.6 h, 390.8 h, 448.8 h, 456.4 h, 867.9 h and 942.9 h
for the peers that ever got there, and since cc281c3c3 only a 'verified' peer
may run a shared agent over A2A.  The round has a 30 s budget and a resume
cursor over every active row; one unreachable row costs the 5 s guardrail GET
plus the challenge's connect timeout, and the log shows the budget spent on
ONE peer ("hit its 30s budget after 1/3 peers").  So a new honest peer waited
behind every stale row the cursor had not reached yet.

The fix ranks peers that spoke for themselves recently (a key on file, seen
within the stale threshold) and are not yet verified ahead of the cursor, in
the same round, through the same IntegrityService challenge.  No new trust
path: 'verified' is still written only by evaluate_challenge_response.

Driven here with the REAL gossip object, the REAL announce, the REAL
_integrity_round and IntegrityService, and the peer's REAL handle_challenge
signed by its own key.  Only the network is simulated, and the clock: every
call to an unreachable row costs its timeout on a fake clock.
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
from integrations.social.integrity_service import IntegrityService
from integrations.social.models import Base, PeerNode, db_session, get_engine

HONEST_URL = 'http://198.51.100.40:6777'


class _Clock:
    def __init__(self):
        self.now = 1_900_000_000.0

    def time(self):
        return self.now

    def __getattr__(self, name):            # sleep, monotonic, ...: the real module
        import time as _t
        return getattr(_t, name)


@pytest.fixture
def world(monkeypatch):
    Base.metadata.create_all(get_engine())
    gossip = pd.gossip
    clock = _Clock()
    monkeypatch.setattr(pd, 'time', clock)
    monkeypatch.setattr(gossip, '_running', True)
    monkeypatch.setattr(gossip, '_auto_federate_peer', lambda *a, **k: None)
    monkeypatch.setattr(gossip, '_integrity_cursor', 0, raising=False)
    monkeypatch.setenv('HEVOLVE_INTEGRITY_ROUND_BUDGET_S', '30')
    # The honest peer signs with this process's key (a fresh one).
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(ni, '_private_key', priv)
    monkeypatch.setattr(ni, '_public_key', priv.public_key())

    def audit_get(url, timeout=None, **kw):
        if url.startswith(HONEST_URL):
            from security.hive_guardrails import get_guardrail_hash
            return types.SimpleNamespace(
                status_code=200,
                json=lambda: {'guardrail_hash': get_guardrail_hash()})
        clock.now += 5                        # connect timeout, unreachable row
        raise requests.ConnectionError('unreachable')
    monkeypatch.setattr(pd, 'pooled_get', audit_get)

    posts = []

    def challenge_post(url, json=None, timeout=None, **kw):
        posts.append(url.split('/api/')[0])
        if url.startswith(HONEST_URL):
            with db_session() as peer_db:
                out = IntegrityService.handle_challenge(peer_db, json)
            return types.SimpleNamespace(
                status_code=200, json=lambda: {'success': True, **out})
        clock.now += 5
        raise requests.ConnectionError('unreachable')
    monkeypatch.setattr(isvc, 'pooled_post', challenge_post)
    yield types.SimpleNamespace(gossip=gossip, clock=clock, posts=posts)
    with db_session() as db:
        db.query(PeerNode).filter(PeerNode.node_id.like('livetest-%')).delete(
            synchronize_session=False)


def _backlog(n, **over):
    """Rows the cursor still has to visit: active, keyed, never verified, not
    heard from for hours, and not answering (the 10.1.x one-shot identities
    and wrong-port rows that made 81% of challenges time out)."""
    old = datetime.utcnow() - timedelta(hours=6)
    with db_session() as db:
        for i in range(n):
            row = dict(node_id=f'livetest-backlog-{i:03d}-{uuid.uuid4().hex[:6]}',
                       url=f'http://10.1.{i // 250}.{i % 250 + 1}:6777',
                       status='active', integrity_status='unverified',
                       public_key=Ed25519PrivateKey.generate().public_key()
                       .public_bytes_raw().hex(),
                       first_seen=old, last_seen=old)
            row.update(over)
            db.add(PeerNode(**row))


def _backlog_urls_in_query_order():
    """The order the round reads active rows in (the cursor walks this)."""
    with db_session() as db:
        return [p.url for p in db.query(PeerNode).filter(
            PeerNode.status == 'active',
            PeerNode.integrity_status != 'banned').all()
            if p.node_id.startswith('livetest-backlog-')]


def _announce_honest(gossip):
    from security.hive_guardrails import get_guardrail_hash
    node_id = f'livetest-honest-{uuid.uuid4().hex[:8]}'
    info = {'node_id': node_id, 'url': HONEST_URL, 'name': 'livetest-honest',
            'version': '1.0.0', 'public_key': ni.get_public_key_hex(),
            'guardrail_hash': get_guardrail_hash(),
            'code_hash': 'livetest-unregistered-desktop-build',
            'timestamp': 1_900_000_000, 'tier': 'flat'}
    info['signature'] = ni.sign_json_payload(info)
    reasons = []
    assert gossip.handle_announce(info, reasons=reasons) is True, reasons
    return node_id


def _status(node_id):
    with db_session() as db:
        return db.query(PeerNode).filter_by(node_id=node_id).one().integrity_status


def test_a_peer_that_just_announced_is_verified_in_the_next_round(world):
    _backlog(12)
    honest = _announce_honest(world.gossip)
    assert _status(honest) == 'unverified'
    world.gossip._integrity_round()
    assert _status(honest) == 'verified'


def test_the_backlog_resumes_exactly_where_it_stopped(world):
    """Ranking a new peer first must neither freeze the cursor over the rest
    nor make it skip a row: round 2 starts at the first backlog row round 1
    did not reach."""
    _backlog(12)
    _announce_honest(world.gossip)
    world.gossip._integrity_round()
    backlog = [u for u in _backlog_urls_in_query_order()]
    round1 = [u for u in world.posts if u != HONEST_URL]
    assert round1 and round1 == backlog[:len(round1)]
    world.posts.clear()
    world.gossip._integrity_round()
    round2 = [u for u in world.posts if u != HONEST_URL]
    assert round2 and round2[0] == backlog[len(round1)], (round1, round2)


def test_a_keyless_row_is_not_ranked_first(world):
    """A relayed hint (no key on file) never spoke for itself: it waits for
    the cursor like any other row, so hearsay cannot jump the queue, even
    when it is the newest row in the table."""
    _backlog(12)
    honest = _announce_honest(world.gossip)
    now = datetime.utcnow() + timedelta(seconds=1)
    _backlog(4, public_key='', first_seen=now, last_seen=now)
    world.gossip._integrity_round()
    assert _status(honest) == 'verified'


def test_the_newest_live_peer_goes_first(world):
    """Several never-challenged peers: newest first, so a burst of one-shot
    identities that announced earlier cannot spend the budget ahead of the
    peer that is announcing right now."""
    _backlog(12)
    earlier = datetime.utcnow() - timedelta(seconds=120)
    _backlog(4, first_seen=earlier, last_seen=earlier)
    honest = _announce_honest(world.gossip)
    world.gossip._integrity_round()
    assert _status(honest) == 'verified'


def test_a_peer_already_challenged_waits_for_the_cursor(world):
    """One prompt attempt: a peer whose first challenge already happened
    (and, say, timed out) goes back to the cursor, so an unreachable live
    announcer cannot take budget every round."""
    _backlog(12)
    honest = _announce_honest(world.gossip)
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=honest).one().last_challenge_at =             datetime.utcnow() - timedelta(minutes=10)
    world.gossip._integrity_round()
    assert _status(honest) == 'unverified'


def test_a_peer_not_heard_from_recently_waits_for_the_cursor(world):
    """Ranking is for LIVE peers: a keyed, unverified row whose last announce
    is older than the stale threshold does not jump the queue."""
    _backlog(12)
    honest = _announce_honest(world.gossip)
    with db_session() as db:
        row = db.query(PeerNode).filter_by(node_id=honest).one()
        row.last_seen = datetime.utcnow() - timedelta(
            seconds=world.gossip.stale_threshold + 60)
    world.gossip._integrity_round()
    assert _status(honest) == 'unverified'


def test_a_peer_that_passed_is_not_ranked_first_again(world):
    """The answered path returned before last_challenge_at was stamped, so a
    peer that PASSED still read as never challenged and jumped the queue in
    every later round.  The stamp is now written when the challenge is
    issued, so round 2 leaves the verified peer to the cursor."""
    _backlog(12)
    honest = _announce_honest(world.gossip)
    world.gossip._integrity_round()
    assert _status(honest) == 'verified'
    with db_session() as db:
        assert db.query(PeerNode).filter_by(
            node_id=honest).one().last_challenge_at is not None
    world.posts.clear()
    world.gossip._integrity_round()
    assert world.posts and world.posts[0] != HONEST_URL, world.posts
