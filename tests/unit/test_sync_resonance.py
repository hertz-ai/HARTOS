"""P3: resonance wallet (financial PII). Central is a BACKUP only — LWW by
updated_at means the node's newer balance ALWAYS wins (no earnings loss) — and
replication is gated on cloud_egress consent. Keyed by user_id (natural key).
100% of added lines. Behavioural; boundary mocked.

    python -m pytest tests/unit/test_sync_resonance.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


class _FakeWallet:
    __tablename__ = 'resonance_wallets'

    def __init__(self, **kw):
        for f in ('id', 'user_id', 'pulse', 'spark', 'spark_lifetime', 'signal',
                  'level', 'level_title', 'xp', 'xp_next_level', 'streak_days',
                  'streak_best', 'last_active_date', 'season_pulse', 'season_spark',
                  'updated_at'):
            setattr(self, f, kw.get(f))

    def to_dict(self):
        return {'user_id': self.user_id, 'pulse': self.pulse, 'spark': self.spark,
                'spark_lifetime': self.spark_lifetime, 'signal': self.signal,
                'level': self.level, 'season_spark': self.season_spark}


class _FakeQ:
    def __init__(self, store, key):
        self._s, self._key, self._k = store, key, None

    def filter_by(self, **kw):
        self._k = kw.get(self._key); return self

    def first(self):
        return self._s.get(self._k)


class _FakeDB:
    def __init__(self, key='user_id'):
        self.store, self._key = {}, key

    def query(self, _m):
        return _FakeQ(self.store, self._key)

    def add(self, o):
        self.store[getattr(o, self._key)] = o

    def flush(self):
        pass


def _wallet(**kw):
    base = dict(user_id='u1', spark=100, pulse=5, spark_lifetime=100,
                season_spark=0, season_pulse=0, updated_at=None)
    base.update(kw)
    return _FakeWallet(**base)


# ── PRODUCER (cloud_egress gate, includes updated_at for LWW) ──
def test_consented_wallet_backed_up(monkeypatch):
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))
    w = _wallet()
    w.updated_at = types.SimpleNamespace(isoformat=lambda: '2026-06-21T10:00:00')
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True) as chk:
        out = se.SyncEngine.queue_entity(None, w)
    assert out == 'q1'
    _, _, op, msg = q.call_args.args
    assert op == 'sync_resonance' and msg['data']['user_id'] == 'u1'
    assert msg['data']['spark'] == 100 and msg['data']['updated_at'] == '2026-06-21T10:00:00'
    cargs, ckw = chk.call_args.args, chk.call_args.kwargs
    assert cargs[1] == 'u1' and cargs[2] == 'cloud_egress' and ckw.get('scope') == 'social_sync'


def test_unconsented_wallet_not_backed_up():
    with patch.object(se.SyncEngine, 'queue') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=False):
        assert se.SyncEngine.queue_entity(None, _wallet()) is None
    q.assert_not_called()


# ── RECEIVER (upsert by user_id + LWW) ──
def test_receiver_upserts_wallet_by_user_id(monkeypatch):
    monkeypatch.setattr(se, '_resonance_model', lambda: _FakeWallet)
    db = _FakeDB(key='user_id')
    se.SyncEngine._handle_sync_resonance(
        db, {'data': {'user_id': 'u1', 'spark': 250, 'pulse': 9}})
    assert db.store['u1'].spark == 250 and db.store['u1'].pulse == 9


def test_receiver_lww_keeps_newer_local_balance(monkeypatch):
    monkeypatch.setattr(se, '_resonance_model', lambda: _FakeWallet)
    db = _FakeDB(key='user_id')
    db.store['u1'] = _FakeWallet(user_id='u1', spark=500, updated_at='2026-06-21T12:00:00')
    se.SyncEngine._handle_sync_resonance(db, {'data': {
        'user_id': 'u1', 'spark': 100, 'updated_at': '2026-06-20T09:00:00'}})
    assert db.store['u1'].spark == 500          # stale backup did NOT clobber earnings


def test_resonance_model_resolver():
    assert se._resonance_model().__tablename__ == 'resonance_wallets'


# ── HOOK (every balance change backs up the wallet) ──
def test_award_spark_backs_up_wallet(monkeypatch):
    from integrations.social.resonance_engine import ResonanceService
    fired = []
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.append(obj)))
    w = _wallet()
    monkeypatch.setattr(ResonanceService, 'get_or_create_wallet',
                        staticmethod(lambda db, uid: w))

    class _DB:
        def add(self, o): pass
        def flush(self): pass

    ResonanceService.award_spark(_DB(), 'u1', 50, 'test')
    assert fired and fired[0] is w               # wallet backed up on the spark award
    assert w.spark == 150                        # 100 + 50
