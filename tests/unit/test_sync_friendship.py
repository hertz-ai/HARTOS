"""P3: friendship (PII social graph) — friendships is a raw-SQL table (no
model), registered as a dict-based entity with a portable upsert. Central backup
is gated on the initiator's cloud_egress consent. 100% of added lines.
Behavioural; boundary mocked.

    python -m pytest tests/unit/test_sync_friendship.py --noconftest -q
"""
from unittest.mock import patch
import types

import integrations.social.sync_engine as se


class _Res:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _RawDB:
    """Minimal raw-SQL friendships fake: SELECT-id existence + INSERT/UPDATE."""

    def __init__(self, existing=()):
        self.ids, self.sql = set(existing), []

    def execute(self, stmt, params=None):
        s = ' '.join(str(stmt).split())
        p = params or {}
        self.sql.append(s)
        if s.startswith('SELECT id FROM friendships'):
            fid = p.get('id')
            return _Res((fid,) if fid in self.ids else None)
        if s.startswith('INSERT INTO friendships'):
            self.ids.add(p.get('id'))
        return _Res(None)

    def commit(self):
        pass


def _fr(**kw):
    base = dict(id='f1', user_a_id='ua', user_b_id='ub',
                status='pending', initiator_id='ua')
    base.update(kw)
    return base


# ── PRODUCER (cloud_egress gate on the initiator) ──
def test_consented_friendship_queued(monkeypatch):
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True) as chk:
        out = se.SyncEngine.queue_entity(None, _fr())
    assert out == 'q1'
    _, _, op, msg = q.call_args.args
    assert op == 'sync_friendship' and msg['data']['id'] == 'f1'
    cargs, ckw = chk.call_args.args, chk.call_args.kwargs
    assert cargs[1] == 'ua' and cargs[2] == 'cloud_egress' and ckw.get('scope') == 'social_sync'


def test_unconsented_friendship_not_queued():
    with patch.object(se.SyncEngine, 'queue') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=False):
        assert se.SyncEngine.queue_entity(None, _fr()) is None
    q.assert_not_called()


# ── RECEIVER (portable raw-SQL upsert) ──
def test_receiver_inserts_new_friendship():
    db = _RawDB()
    assert se.SyncEngine._handle_sync_friendship(db, {'data': _fr()}) == 'f1'
    assert any('INSERT INTO friendships' in s for s in db.sql)
    assert 'f1' in db.ids


def test_receiver_updates_existing_friendship():
    db = _RawDB(existing=['f1'])
    assert se.SyncEngine._handle_sync_friendship(
        db, {'data': {'id': 'f1', 'status': 'active',
                      'accepted_at': '2026-06-21T10:00:00'}}) == 'f1'
    assert any('UPDATE friendships' in s for s in db.sql)
    assert not any('INSERT INTO friendships' in s for s in db.sql)


def test_receiver_no_id_is_noop():
    assert se.SyncEngine._handle_sync_friendship(_RawDB(), {'data': {}}) is None


# ── _sync_friendship helper ──
def test_sync_friendship_reads_and_queues(monkeypatch):
    from integrations.social.friend_service import FriendService
    fired = []
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.append(obj)))

    class _DB:
        def execute(self, stmt, params=None):
            return _Res(('f1', 'ua', 'ub', 'pending', 'ua', 'now', None))

    FriendService._sync_friendship(_DB(), 'f1')
    assert fired and fired[0]['id'] == 'f1' and fired[0]['initiator_id'] == 'ua'
    assert fired[0]['accepted_at'] is None


def test_sync_friendship_missing_row_noop(monkeypatch):
    from integrations.social.friend_service import FriendService
    fired = []
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.append(obj)))

    class _DB:
        def execute(self, stmt, params=None):
            return _Res(None)

    FriendService._sync_friendship(_DB(), 'nope')
    assert fired == []


# ── HOOKS (send_request + accept both up-sync) ──
def test_send_request_fires_sync(monkeypatch):
    from integrations.social.friend_service import FriendService
    called = []
    monkeypatch.setattr(FriendService, '_sync_friendship',
                        staticmethod(lambda db, fid: called.append(fid)))
    monkeypatch.setattr(FriendService, '_notify_request', staticmethod(lambda *a, **k: None))
    monkeypatch.setattr('integrations.social.friend_service._is_blocked_either_way',
                        lambda db, a, b: False)

    class _DB:
        def execute(self, stmt, params=None):
            return _Res(None)            # no existing row

        def commit(self):
            pass

    out = FriendService.send_request(_DB(), 'ua', 'ub')
    assert out['status'] == 'pending'
    assert called and called[0] == out['id']


def test_accept_fires_sync(monkeypatch):
    from integrations.social.friend_service import FriendService
    called = []
    monkeypatch.setattr(FriendService, '_sync_friendship',
                        staticmethod(lambda db, fid: called.append(fid)))
    monkeypatch.setattr('integrations.social.services.NotificationService.create',
                        staticmethod(lambda *a, **k: None))

    class _DB:
        bind = None                      # → dialect 'sqlite'

        def execute(self, stmt, params=None):
            return _Res(('ua', 'ub', 'pending', 'ub'))   # initiator=ub, accepting=ua

        def commit(self):
            pass

    out = FriendService._accept_internal(_DB(), 'f1', accepting_user_id='ua')
    assert out['status'] == 'active'
    assert called and called[0] == 'f1'
