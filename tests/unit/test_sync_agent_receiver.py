"""Receiver half of the PUBLIC agent up-sync (gap #4): the un-stubbed
register_agent branch lands a synced agent as a central/regional User row
(sync_engine._handle_sync_agent), mirroring _handle_sync_user's upsert-by-id
contract.

Behavioural (in-memory User stand-in; call the REAL _handle_sync_agent /
receive_sync_batch; assert rows created/updated, api_token NOT regenerated,
malformed payloads no-op).  No grep tests.
"""
import os
import sys
import types
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.social.sync_engine import SyncEngine  # noqa: E402
from integrations.social.federation import federation  # noqa: E402


class _FakeUser:
    """Stand-in for the social User row the receiver creates/updates."""
    def __init__(self, **kw):
        for f in ('id', 'username', 'display_name', 'bio', 'user_type',
                  'agent_id', 'owner_id', 'handle', 'local_name', 'api_token',
                  'is_verified'):
            setattr(self, f, kw.get(f))


class _FakeQuery:
    def __init__(self, store):
        self._store = store
        self._key = None

    def filter_by(self, **kw):
        self._key = kw.get('id')
        return self

    def first(self):
        return self._store.get(self._key)


class _FakeDB:
    def __init__(self):
        self.store = {}

    def query(self, _model):
        return _FakeQuery(self.store)

    def add(self, obj):
        self.store[obj.id] = obj

    def flush(self):
        pass


def _install_user_and_token(monkeypatch):
    import integrations.social.models as m
    monkeypatch.setattr(m, 'User', _FakeUser, raising=False)
    import integrations.social.auth as auth
    monkeypatch.setattr(auth, 'generate_api_token', lambda: 'api-tok', raising=False)


_AGENT_PAYLOAD = {
    'type': 'agent',
    'origin_node_id': 'nodeA',
    'agent': {
        'id': 'agent-9', 'username': 'swift.falcon.sathi',
        'display_name': 'swift falcon', 'bio': 'a helper', 'user_type': 'agent',
        'agent_id': '65_0', 'owner_id': 'owner-1',
    },
}


def test_receiver_creates_agent_user(monkeypatch):
    _install_user_and_token(monkeypatch)
    db = _FakeDB()
    SyncEngine._handle_sync_agent(db, _AGENT_PAYLOAD)

    row = db.store['agent-9']
    assert row.username == 'swift.falcon.sathi'
    assert row.user_type == 'agent'
    assert row.agent_id == '65_0'
    assert row.owner_id == 'owner-1'
    # SECURITY: a synced agent is a discoverable MIRROR, not a credentialed
    # identity — no api_token is minted and it is NOT auto-verified (the
    # credential + verification stay on the agent's home node).
    assert row.api_token is None
    assert row.is_verified is False


def test_receiver_idempotent_upsert_by_id_does_not_regenerate_token(monkeypatch):
    """A second sync of the same agent id UPDATES (no duplicate), and the
    api_token set on create is NEVER overwritten (mirrors _handle_sync_user)."""
    _install_user_and_token(monkeypatch)
    db = _FakeDB()
    # seed an existing agent with a local token
    db.store['agent-9'] = _FakeUser(id='agent-9', username='swift.falcon.sathi',
                                    display_name='old name', user_type='agent',
                                    api_token='LOCAL-SECRET', is_verified=True)

    changed = dict(_AGENT_PAYLOAD)
    changed['agent'] = dict(_AGENT_PAYLOAD['agent'], display_name='new name',
                            bio='updated bio')
    SyncEngine._handle_sync_agent(db, changed)

    assert len(db.store) == 1                       # no duplicate
    row = db.store['agent-9']
    assert row.display_name == 'new name'           # sync-owned field updated
    assert row.bio == 'updated bio'
    assert row.api_token == 'LOCAL-SECRET'          # NEVER overwritten


def test_receiver_noop_on_non_agent_type(monkeypatch):
    _install_user_and_token(monkeypatch)
    db = _FakeDB()
    SyncEngine._handle_sync_agent(db, {'type': 'new_post', 'agent': {'id': 'x'}})
    assert db.store == {}                            # defensive no-op


def test_receiver_noop_on_missing_agent_id_or_username(monkeypatch):
    _install_user_and_token(monkeypatch)
    db = _FakeDB()
    SyncEngine._handle_sync_agent(db, {'type': 'agent', 'agent': {'username': 'x'}})
    SyncEngine._handle_sync_agent(db, {'type': 'agent', 'agent': {'id': 'y'}})
    assert db.store == {}


def test_receiver_via_batch_dispatch_and_idempotency(monkeypatch):
    """receive_sync_batch routes a register_agent item to the receiver and marks
    it processed; a duplicate (already-completed) item is skipped, not re-applied."""
    _install_user_and_token(monkeypatch)

    # a DB that supports BOTH the SyncQueue idempotency lookup (filter_by(id=).
    # first()) and the receiver's User upsert (query(User)...).
    store = {}

    class _Q:
        def __init__(self, model):
            self.model = model
            self._key = None

        def filter_by(self, **kw):
            self._key = kw.get('id')
            return self

        def first(self):
            # SyncQueue idempotency check → no prior record (not completed)
            if getattr(self.model, '__name__', '') == 'SyncQueue':
                return None
            return store.get(self._key)

    class _DB2:
        def query(self, model):
            return _Q(model)

        def add(self, obj):
            store[obj.id] = obj

        def flush(self):
            pass

    res = SyncEngine.receive_sync_batch(_DB2(), [
        {'id': 'item_1', 'operation_type': 'register_agent', 'payload': _AGENT_PAYLOAD},
    ])
    assert 'item_1' in res['processed']
    assert res['errors'] == []
    assert store['agent-9'].user_type == 'agent'    # the agent landed


def test_owner_id_round_trips_even_when_to_dict_omits_it(monkeypatch):
    """The REAL contract the per-half tests miss: the canonical User.to_dict()
    does NOT emit owner_id, so the producer envelope must re-attach it or the
    central mirror lands ownerless.  Build the envelope with the REAL
    _agent_message, feed it to the REAL _handle_sync_agent, assert owner_id
    persisted AND that the mirror is non-credentialed/unverified."""
    import integrations.social.peer_discovery as pd
    monkeypatch.setattr(
        pd, 'gossip',
        types.SimpleNamespace(node_id='nodeA', base_url='http://a', node_name='A'),
        raising=False)
    monkeypatch.setattr(federation, '_agent_skill_summary', lambda db, user: [])

    # an agent whose to_dict() OMITS owner_id (mirrors the real SocialUser.to_dict)
    agent = types.SimpleNamespace(
        id='agent-9', username='swift.falcon.sathi', user_type='agent',
        owner_id='owner-7',
        to_dict=lambda: {'id': 'agent-9', 'username': 'swift.falcon.sathi',
                         'display_name': 'swift falcon', 'user_type': 'agent',
                         'agent_id': '65_0'})          # NOTE: no owner_id

    assert 'owner_id' not in agent.to_dict()           # precondition: dropped
    envelope = federation._agent_message('DB', agent)
    assert envelope['agent']['owner_id'] == 'owner-7'  # producer re-attached it

    import integrations.social.models as m
    monkeypatch.setattr(m, 'User', _FakeUser, raising=False)
    db = _FakeDB()
    SyncEngine._handle_sync_agent(db, envelope)
    row = db.store['agent-9']
    assert row.owner_id == 'owner-7'      # round-trip preserved end-to-end
    assert row.api_token is None          # still a non-credentialed mirror
    assert row.is_verified is False
