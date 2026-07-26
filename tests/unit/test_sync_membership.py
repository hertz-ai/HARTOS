"""P3: community_membership (join/leave) — same registry pattern, gated to
PUBLIC communities only (private membership stays private). 100% of the added
lines. Behavioural: real queue_entity / _handle_sync_membership /
_membership_gate; boundary mocked.

    python -m pytest tests/unit/test_sync_membership.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


class _FakeMembership:
    __tablename__ = 'community_memberships'

    def __init__(self, **kw):
        for f in ('id', 'user_id', 'community_id', 'role', 'created_at'):
            setattr(self, f, kw.get(f))

    def to_dict(self):
        return {'id': self.id, 'user_id': self.user_id,
                'community_id': self.community_id, 'role': self.role,
                'created_at': self.created_at}


class _FakeCommunity:
    __tablename__ = 'communities'

    def __init__(self, id, is_private):
        self.id, self.is_private = id, is_private


class _FakeQ:
    def __init__(self, store):
        self._s, self._k = store, None

    def filter_by(self, **kw):
        self._k = kw.get('id'); return self

    def first(self):
        return self._s.get(self._k)


class _FakeDB:
    def __init__(self, communities=None, members=None):
        self.s = {'communities': communities or {},
                  'community_memberships': members or {}}

    def query(self, model):
        return _FakeQ(self.s.get(getattr(model, '__tablename__', ''), {}))

    def add(self, o):
        self.s['community_memberships'][o.id] = o

    def flush(self):
        pass


def _membership(**kw):
    base = dict(id='m1', user_id='u1', community_id='c1', role='member')
    base.update(kw)
    return _FakeMembership(**base)


def _patch_models(monkeypatch):
    import integrations.social.models as m
    monkeypatch.setattr(se, '_membership_model', lambda: _FakeMembership)
    monkeypatch.setattr(m, 'Community', _FakeCommunity, raising=False)


# ── PRODUCER (public-community gate) ──
def test_membership_of_public_community_queued(monkeypatch):
    _patch_models(monkeypatch)
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))
    db = _FakeDB(communities={'c1': _FakeCommunity('c1', is_private=False)})
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q:
        out = se.SyncEngine.queue_entity(db, _membership())
    assert out == 'q1'
    _, tier, op, msg = q.call_args.args
    assert op == 'sync_membership' and msg['data']['community_id'] == 'c1'


def test_membership_of_private_community_not_queued(monkeypatch):
    _patch_models(monkeypatch)
    db = _FakeDB(communities={'c1': _FakeCommunity('c1', is_private=True)})
    with patch.object(se.SyncEngine, 'queue') as q:
        assert se.SyncEngine.queue_entity(db, _membership()) is None
    q.assert_not_called()


def test_membership_missing_community_fail_closed(monkeypatch):
    _patch_models(monkeypatch)
    db = _FakeDB(communities={})            # community gone → gate fails closed
    with patch.object(se.SyncEngine, 'queue') as q:
        assert se.SyncEngine.queue_entity(db, _membership()) is None
    q.assert_not_called()


# ── RECEIVER ──
def test_receiver_upserts_membership(monkeypatch):
    _patch_models(monkeypatch)
    db = _FakeDB()
    se.SyncEngine._handle_sync_membership(
        db, {'data': {'id': 'm1', 'user_id': 'u1', 'community_id': 'c1', 'role': 'member'}})
    row = db.s['community_memberships']['m1']
    assert row.user_id == 'u1' and row.role == 'member'


def test_membership_model_resolver():
    assert se._membership_model().__tablename__ == 'community_memberships'


# ── HOOK ──
def test_join_routes_through_unified_producer(monkeypatch):
    from integrations.social.services import CommunityService
    fired = {}
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.setdefault('obj', obj)))
    monkeypatch.setattr('integrations.social.services._publish_realtime',
                        lambda *a, **k: None)

    class _DB:
        def query(self, _m): return self
        def filter(self, *a, **k): return self
        def first(self): return None
        def execute(self, *a, **k): pass
        def add(self, o): pass
        def flush(self): pass

    user = types.SimpleNamespace(id='u1', user_type='human')
    community = types.SimpleNamespace(id='c1', member_count=0)
    CommunityService.join(_DB(), user, community)
    assert getattr(fired.get('obj'), 'community_id', None) == 'c1'   # membership up-synced
