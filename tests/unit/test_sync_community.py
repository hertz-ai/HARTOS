"""P3: the `community` entity rides the SAME registry / producer / receiver — a
registration, not new dispatch code. Covers EVERY added line: the generic
upsert receiver (_apply_synced_row create/update/LWW/no-id), the community
gate/serialize, the generic _entity_message envelope, the producer hook, and the
model resolver — to 100% of the lines this phase adds.

Behavioural: real queue_entity / _handle_sync_community / _apply_synced_row;
db + SyncEngine.queue + gossip mocked at the boundary.

    python -m pytest tests/unit/test_sync_community.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


class _FakeCommunity:
    __tablename__ = 'communities'

    def __init__(self, **kw):
        for f in ('id', 'name', 'display_name', 'description', 'rules',
                  'icon_url', 'banner_url', 'creator_id', 'is_default',
                  'is_private', 'member_count', 'post_count', 'created_at'):
            setattr(self, f, kw.get(f))

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'display_name': self.display_name,
                'is_private': self.is_private, 'created_at': self.created_at}


class _FakeQ:
    def __init__(self, store):
        self._store, self._k = store, None

    def filter_by(self, **kw):
        self._k = kw.get('id'); return self

    def first(self):
        return self._store.get(self._k)


class _FakeDB:
    def __init__(self):
        self.store = {}

    def query(self, _m):
        return _FakeQ(self.store)

    def add(self, o):
        self.store[o.id] = o

    def flush(self):
        pass


def _community(**kw):
    base = dict(id='c1', name='hartos', display_name='HART OS', is_private=False)
    base.update(kw)
    return _FakeCommunity(**base)


def _patch_model(monkeypatch):
    monkeypatch.setattr(se, '_community_model', lambda: _FakeCommunity)


# ── PRODUCER (gate + serialize + queue) ──
def test_public_community_queued_via_unified_producer(monkeypatch):
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q:
        out = se.SyncEngine.queue_entity(None, _community())
    assert out == 'q1'
    _, tier, op, msg = q.call_args.args
    assert tier == 'central' and op == 'sync_community'
    assert msg['type'] == 'community' and msg['data']['id'] == 'c1'   # _entity_message ran


def test_private_community_not_queued():
    with patch.object(se.SyncEngine, 'queue') as q:
        out = se.SyncEngine.queue_entity(None, _community(is_private=True))
    assert out is None
    q.assert_not_called()


# ── RECEIVER (generic upsert) ──
def test_receiver_creates_community(monkeypatch):
    _patch_model(monkeypatch)
    db = _FakeDB()
    se.SyncEngine._handle_sync_community(
        db, {'type': 'community',
             'data': {'id': 'c1', 'name': 'hartos', 'display_name': 'HART OS'}})
    assert db.store['c1'].name == 'hartos' and db.store['c1'].display_name == 'HART OS'


def test_receiver_updates_existing_community(monkeypatch):
    _patch_model(monkeypatch)
    db = _FakeDB()
    db.store['c1'] = _FakeCommunity(id='c1', name='hartos', display_name='old')
    se.SyncEngine._handle_sync_community(
        db, {'data': {'id': 'c1', 'display_name': 'new', 'description': 'd'}})
    assert len(db.store) == 1
    assert db.store['c1'].display_name == 'new' and db.store['c1'].description == 'd'


def test_apply_synced_row_lww_skips_stale_write():
    db = _FakeDB()
    db.store['c1'] = _FakeCommunity(id='c1', name='current')
    db.store['c1'].created_at = '2026-06-21T10:00:00'
    out = se.SyncEngine._apply_synced_row(
        db, _FakeCommunity,
        {'id': 'c1', 'name': 'stale', 'created_at': '2026-06-20T09:00:00'},
        ['name'], ts_field='created_at')
    assert out == 'c1'
    assert db.store['c1'].name == 'current'      # stale write did NOT overwrite (LWW)


def test_apply_synced_row_no_id_is_noop():
    assert se.SyncEngine._apply_synced_row(
        _FakeDB(), _FakeCommunity, {}, ['name']) is None


def test_community_model_resolver():
    assert se._community_model().__tablename__ == 'communities'   # real lazy import


# ── PRODUCER HOOK ──
def test_community_create_fires_up_sync(monkeypatch):
    from integrations.social.services import CommunityService
    fired = {}
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.setdefault('obj', obj)))

    class _DB:
        def query(self, _m): return self
        def filter(self, *a, **k): return self
        def first(self): return None
        def add(self, o): pass
        def flush(self): pass

    creator = types.SimpleNamespace(id='u1')
    community = CommunityService.create(_DB(), creator, 'hartos', is_private=False)
    assert fired.get('obj') is community         # create() routed through queue_entity
