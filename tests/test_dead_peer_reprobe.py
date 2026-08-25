"""A dead peer row must be re-probed and revived, not excluded forever.

Live incident 2026-08-25 (desktop, build 16): box9 (192.168.0.9:6777) was
alive and serving, but the desktop's broadcast never reached it — its
peer_nodes row had aged to 'dead' during an outage window, and
_health_check_round's query (peer_discovery.py:689,
``filter(PeerNode.status != 'dead')``) excludes dead rows from the ping
loop FOREVER.  A dead row could only come back via an INBOUND announce
(a manual gossip._announce_to_peer is what actually healed it), so two
nodes that age each other out stay partitioned permanently on a LAN with
no central to reintroduce them.

The fix: each health round also re-probes a bounded random batch of dead
rows (bounded because central's table held 673 rows; random so
unrevivable rows cannot starve the rotation).  Proven RED before the fix.
"""
import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social import peer_discovery as pd
from integrations.social.models import Base, PeerNode


@pytest.fixture
def db_session_factory(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    import integrations.social.models as models
    monkeypatch.setattr(models, 'get_db', lambda: factory())
    return factory


def _seed(factory, url, status, node_id, days_old=2):
    db = factory()
    seen = _dt.datetime.utcnow() - _dt.timedelta(days=days_old)
    db.add(PeerNode(node_id=node_id, url=url, status=status,
                    first_seen=seen, last_seen=seen))
    db.commit()
    db.close()


@pytest.fixture
def gossip(monkeypatch):
    g = pd.GossipProtocol()
    monkeypatch.setattr(g, '_heartbeat', lambda: None)

    class _NoBackoff:
        def prune_expired(self):
            pass

    g._peer_backoff = _NoBackoff()
    g._running = True
    return g


def test_dead_peer_revived_when_reachable(db_session_factory, gossip, monkeypatch):
    _seed(db_session_factory, 'http://10.9.9.9:6100', 'dead', 'dead-node-1')
    monkeypatch.setattr(gossip, '_ping_peer', lambda url: True)

    gossip._health_check_round()

    db = db_session_factory()
    row = db.query(PeerNode).filter_by(node_id='dead-node-1').one()
    assert row.status == 'active', (
        'a reachable dead peer must be revived by the health check')
    db.close()


def test_dead_reprobe_is_bounded(db_session_factory, gossip, monkeypatch):
    for i in range(12):
        _seed(db_session_factory, f'http://10.9.9.{i + 1}:6100', 'dead',
              f'dead-node-{i:02d}')
    pinged = []
    monkeypatch.setattr(gossip, '_ping_peer',
                        lambda url: pinged.append(url) or False)

    gossip._health_check_round()

    assert 0 < len(pinged) <= 5, (
        f'one round must probe a bounded dead batch, got {len(pinged)}')


def test_unreachable_dead_peer_stays_dead(db_session_factory, gossip, monkeypatch):
    _seed(db_session_factory, 'http://10.9.9.9:6100', 'dead', 'dead-node-1')
    monkeypatch.setattr(gossip, '_ping_peer', lambda url: False)

    gossip._health_check_round()

    db = db_session_factory()
    row = db.query(PeerNode).filter_by(node_id='dead-node-1').one()
    assert row.status == 'dead'
    db.close()
