"""A relayed peer-list merge must not hold the SQLite write lock for its
whole length.

Live 2026-09-15 11:28:50-11:29:02 (Nunba boot, hartos bbf9653d3): one
_merge_peer_list call walked about a thousand relayed records in one
session and committed once at the end ("Gossip: merged 14 new peers",
11:29:02).  _merge_peer queries before it adds, so SQLAlchemy's autoflush
opened the write transaction at the second record and held it until that
commit.  The owner clicked "Allow" on the consent card at 11:28:58;
POST /api/social/consent waited out busy_timeout (3s) and failed with
"sqlite3.OperationalError: database is locked", the SPA showed
"Something's off on our end. Try refreshing.", and the card looked dead.

The records are address hints (see _merge_peer_list's docstring), so
nothing needs them committed together.  Committing each one releases the
lock between records and a concurrent writer waits at most one record.

    python -m pytest tests/unit/test_peer_merge_does_not_hold_the_write_lock.py --noconftest -q
"""
import os
import tempfile
import threading
import time

import pytest


@pytest.fixture
def db_path(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "hevolve_database.db")
    monkeypatch.setenv("HEVOLVE_DB_PATH", p)
    monkeypatch.setenv("HEVOLVE_DB_URL", "sqlite:///" + p.replace("\\", "/"))
    from integrations.social import models
    # Both caches are bound to the old engine; clear them or the test runs
    # against the developer's real DB.
    for attr in ("_engine", "_ENGINE", "_engine_cache", "_SessionLocal"):
        if hasattr(models, attr):
            monkeypatch.setattr(models, attr, None, raising=False)
    models.init_db()
    return p


def test_a_writer_gets_in_while_a_long_merge_runs(db_path, monkeypatch):
    from integrations.social import peer_discovery
    from integrations.social.models import PeerNode, get_db

    gossip = peer_discovery.GossipProtocol.__new__(peer_discovery.GossipProtocol)
    gossip.node_id = "self-node"

    # The DB shape of the real _merge_peer, slowed to 50ms a record: a
    # query (autoflush) then an add.  100 records = 5s, longer than the
    # 3s busy_timeout, which is what the live merge was.
    def slow_merge_peer(db, peer_data, reasons=None, relayed=False, observed_ip=''):
        nid = peer_data['node_id']
        db.query(PeerNode).filter(PeerNode.node_id == nid).first()
        db.add(PeerNode(node_id=nid, url=peer_data['url']))
        time.sleep(0.05)
        return True

    monkeypatch.setattr(gossip, "_merge_peer", slow_merge_peer)
    relayed = [{'node_id': 'peer-%03d' % i, 'url': 'http://10.0.0.%d:5000' % (i % 250)}
               for i in range(100)]

    merge_error = []

    def run_merge():
        try:
            gossip._merge_peer_list(relayed)
        except Exception as e:  # pragma: no cover - reported by the assert below
            merge_error.append(e)

    t = threading.Thread(target=run_merge, daemon=True)
    t.start()
    time.sleep(0.5)          # the merge is well inside its loop

    # The consent grant, as far as the DB is concerned: another session,
    # one row, one commit, while the merge is still running.
    other = get_db()
    t0 = time.time()
    try:
        other.add(PeerNode(node_id='the-owner-click', url='http://127.0.0.1:5000'))
        other.commit()
    finally:
        other.close()
    waited = time.time() - t0
    assert t.is_alive(), "the merge finished before the writer tried; the test measured nothing"
    assert waited < 1.0, "the writer waited %.1fs: the merge holds the write lock" % waited

    t.join(timeout=15)
    assert not merge_error, merge_error
    check = get_db()
    try:
        assert check.query(PeerNode).count() == 101, "every record still lands, plus the writer's row"
    finally:
        check.close()
