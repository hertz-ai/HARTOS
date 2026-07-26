"""P3: encounter (PII + precise location) — central replication is OFF unless
the user opts in via cloud_egress consent, and the producer NEVER includes
lat/lng/location_label. 100% of the added lines. Behavioural: real queue_entity
/ _handle_sync_encounter / _encounter_*; boundary mocked.

    python -m pytest tests/unit/test_sync_encounter.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


class _FakeEncounter:
    __tablename__ = 'encounters'

    def __init__(self, **kw):
        for f in ('id', 'user_a_id', 'user_b_id', 'context_type', 'context_id',
                  'location_label', 'encounter_count', 'bond_level',
                  'is_mutual_aware', 'lat', 'lng'):
            setattr(self, f, kw.get(f))

    def to_dict(self):
        return {'id': self.id, 'user_a_id': self.user_a_id, 'user_b_id': self.user_b_id,
                'context_type': self.context_type, 'context_id': self.context_id,
                'location_label': self.location_label,
                'encounter_count': self.encounter_count, 'bond_level': self.bond_level,
                'is_mutual_aware': self.is_mutual_aware, 'lat': self.lat, 'lng': self.lng}


class _FakeQ:
    def __init__(self, store):
        self._s, self._k = store, None

    def filter_by(self, **kw):
        self._k = kw.get('id'); return self

    def first(self):
        return self._s.get(self._k)


class _FakeDB:
    def __init__(self):
        self.store = {}

    def query(self, _m):
        return _FakeQ(self.store)

    def add(self, o):
        self.store[o.id] = o

    def flush(self):
        pass


def _encounter(**kw):
    base = dict(id='e1', user_a_id='ua', user_b_id='ub', context_type='community',
                context_id='c1', location_label='Cafe', encounter_count=1,
                bond_level=0, is_mutual_aware=False, lat=12.97, lng=77.59)
    base.update(kw)
    return _FakeEncounter(**base)


# ── PRODUCER (cloud_egress gate + location exclusion) ──
def test_consented_encounter_queued_without_location(monkeypatch):
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))
    with patch.object(se.SyncEngine, 'queue', return_value='q1') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True) as chk:
        out = se.SyncEngine.queue_entity(None, _encounter())
    assert out == 'q1'
    _, _, op, msg = q.call_args.args
    assert op == 'sync_encounter'
    data = msg['data']
    assert data['user_a_id'] == 'ua' and data['encounter_count'] == 1
    assert 'lat' not in data and 'lng' not in data and 'location_label' not in data
    # BOTH parties' cloud_egress consent is checked (not just user_a)
    checked = {c.args[1] for c in chk.call_args_list}
    assert checked == {'ua', 'ub'}
    assert all(c.args[2] == 'cloud_egress' and c.kwargs.get('scope') == 'social_sync'
               for c in chk.call_args_list)


def test_unconsented_encounter_not_queued():
    with patch.object(se.SyncEngine, 'queue') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=False):
        assert se.SyncEngine.queue_entity(None, _encounter()) is None
    q.assert_not_called()


def test_encounter_requires_BOTH_parties_consent():
    # F7: user_a consented, user_b did NOT → the encounter must NOT leave the
    # node (gating on user_a alone previously leaked user_b's participation).
    def _consent(db, uid, ctype, scope='*', agent_id=None):
        return uid == 'ua'
    with patch.object(se.SyncEngine, 'queue') as q, \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  side_effect=_consent):
        assert se.SyncEngine.queue_entity(None, _encounter()) is None
    q.assert_not_called()


# ── RECEIVER (location-free upsert) ──
def test_receiver_upserts_encounter_location_free(monkeypatch):
    monkeypatch.setattr(se, '_encounter_model', lambda: _FakeEncounter)
    db = _FakeDB()
    se.SyncEngine._handle_sync_encounter(db, {'data': {
        'id': 'e1', 'user_a_id': 'ua', 'user_b_id': 'ub',
        'context_type': 'community', 'encounter_count': 3, 'bond_level': 1}})
    row = db.store['e1']
    assert row.user_a_id == 'ua' and row.encounter_count == 3 and row.bond_level == 1
    assert row.lat is None          # location never set from sync


def test_encounter_model_resolver():
    assert se._encounter_model().__tablename__ == 'encounters'


# ── HOOKS (both record_encounter paths) ──
def test_record_new_encounter_fires_up_sync(monkeypatch):
    from integrations.social.encounter_service import EncounterService
    fired = []
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.append(obj)))

    class _DB:
        def query(self, _m): return self
        def filter_by(self, **k): return self
        def first(self): return None
        def add(self, o): pass
        def flush(self): pass

    EncounterService.record_encounter(_DB(), 'ua', 'ub', 'community', 'c1')
    assert fired and getattr(fired[0], 'user_a_id', None) == 'ua'


def test_record_existing_encounter_fires_up_sync(monkeypatch):
    from integrations.social.encounter_service import EncounterService
    fired = []
    monkeypatch.setattr(se.SyncEngine, 'queue_entity',
                        staticmethod(lambda db, obj: fired.append(obj)))
    existing = types.SimpleNamespace(encounter_count=4, bond_level=0, latest_at=None,
                                     user_a_id='ua', to_dict=lambda: {'id': 'e1'})

    class _DB:
        def query(self, _m): return self
        def filter_by(self, **k): return self
        def first(self): return existing
        def flush(self): pass

    EncounterService.record_encounter(_DB(), 'ua', 'ub', 'community', 'c1')
    assert fired and fired[0] is existing       # the count-update also up-syncs
