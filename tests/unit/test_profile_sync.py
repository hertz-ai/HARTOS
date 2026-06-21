"""Profile down-sync (#2): pull a centrally-created/edited profile DOWN into
the local social store by feeding the EXISTING ``_handle_sync_user`` receiver —
no parallel writer (core/profile_sync.py).

Behavioural tests (mock the boundary, call the real fn, assert observable
effects — no grep tests, per memory/feedback_no_grep_tests.md):
  - build_user_sync_payload: pure field translation (central name → display_name,
    central id through, user_id = LOCAL uuid, username derivation, FCM/extras).
  - fetch_central_profile: mocked HTTP boundary — JSON on 200, None on
    404/timeout/exception/no-central (never raises).
  - sync_profile: spies on SyncEngine._handle_sync_user, asserts it is called
    ONCE with the translated payload (proves receiver-reuse) and the GATE
    (one central id in → exactly one user handed to the receiver).
  - receiver round-trip: build_user_sync_payload output fed through the REAL
    _handle_sync_user against an in-memory User stand-in materialises the row +
    central-id map + FCM cache (the end-to-end proof the two halves meet).
"""
from __future__ import annotations

import contextlib
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import profile_sync


# ── pure payload builder ─────────────────────────────────────────────────────
def test_build_payload_maps_central_fields_and_keys_local_uuid():
    resp = {
        'user_id': 9003054371,
        'name': 'Ada Lovelace',
        'email_address': 'ada@x.io',
        'phone_number': '9003054371',
        'role': 'student',
        'preferred_language': 'en',
        'FCMtoken': 'tok-abc',
    }
    p = profile_sync.build_user_sync_payload('local-uuid-1', 9003054371, resp)
    # keyed on the LOCAL uuid (what the push path looks up) — NOT the central id
    assert p['user_id'] == 'local-uuid-1'
    assert p['central_user_id'] == '9003054371'
    # central name flows to display_name; username derives from name
    assert p['display_name'] == 'Ada Lovelace'
    assert p['username'] == 'Ada Lovelace'
    # FCM token + profile extras carried in the shape the receiver tolerates
    assert p['fcm_token'] == 'tok-abc'
    assert p['preferred_language'] == 'en'
    assert p['email'] == 'ada@x.io'
    assert p['phone'] == '9003054371'
    assert p['role'] == 'student'


def test_build_payload_username_falls_back_to_central_id_when_no_name():
    """username is hard-required non-empty by the receiver (it early-returns
    otherwise) and central has no username column — empty name must fall back to
    the central id, deterministically."""
    p = profile_sync.build_user_sync_payload('local-2', 555, {'name': '   '})
    assert p['username'] == '555'
    assert p['display_name'] == '555'
    # no FCM token / no extras present → those keys are simply absent (no Nones)
    assert 'fcm_token' not in p
    assert 'preferred_language' not in p


def test_build_payload_tolerates_non_dict_response():
    p = profile_sync.build_user_sync_payload('local-3', 7, None)
    assert p['user_id'] == 'local-3'
    assert p['username'] == '7'           # falls back to the central id
    assert p['central_user_id'] == '7'


# ── fetch (mocked HTTP boundary) ─────────────────────────────────────────────
def _mock_requests(status, body, *, raises=None):
    class _Resp:
        status_code = status

        def json(self):
            return body

    def _get(*a, **k):
        if raises:
            raise raises
        return _Resp()

    return types.SimpleNamespace(get=_get)


def test_fetch_returns_json_on_200(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    monkeypatch.setitem(sys.modules, 'requests',
                        _mock_requests(200, {'user_id': 1, 'name': 'X'}))
    out = profile_sync.fetch_central_profile(1)
    assert out == {'user_id': 1, 'name': 'X'}


def test_fetch_none_on_404(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    monkeypatch.setitem(sys.modules, 'requests',
                        _mock_requests(404, {'detail': 'user not Found'}))
    assert profile_sync.fetch_central_profile(1) is None


def test_fetch_none_on_exception(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    monkeypatch.setitem(sys.modules, 'requests',
                        _mock_requests(200, {}, raises=RuntimeError('boom')))
    assert profile_sync.fetch_central_profile(1) is None  # never raises


def test_fetch_none_without_central_configured(monkeypatch):
    monkeypatch.delenv('HEVOLVE_CENTRAL_URL', raising=False)
    monkeypatch.delenv('HEVOLVE_REGIONAL_URL', raising=False)
    # even with a reachable stub, no central URL → no pull
    monkeypatch.setitem(sys.modules, 'requests',
                        _mock_requests(200, {'user_id': 1}))
    assert profile_sync.fetch_central_profile(1) is None


def test_fetch_none_without_central_user_id(monkeypatch):
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    assert profile_sync.fetch_central_profile('') is None
    assert profile_sync.fetch_central_profile(None) is None


# ── sync_profile feeds the EXISTING receiver (no parallel writer) ────────────
def _patch_db_session(monkeypatch):
    """Patch integrations.social.models.db_session to a no-op CM yielding a
    sentinel DB, so sync_profile's `with db_session() as db:` works without a
    real DB.  Returns the sentinel for assertions."""
    sentinel_db = object()

    @contextlib.contextmanager
    def _cm(commit=True):
        yield sentinel_db

    import integrations.social.models as m
    monkeypatch.setattr(m, 'db_session', _cm, raising=False)
    return sentinel_db


def test_sync_profile_calls_receiver_once_with_translated_payload(monkeypatch):
    """The GATE + receiver-reuse proof: one central id in → _handle_sync_user
    invoked EXACTLY ONCE with the build_user_sync_payload output (keyed on the
    local uuid).  No second writer, no batch."""
    monkeypatch.setenv('HEVOLVE_CENTRAL_URL', 'http://central')
    monkeypatch.setattr(profile_sync, 'fetch_central_profile',
                        lambda cid, **k: {'user_id': cid, 'name': 'Grace',
                                          'FCMtoken': 'tok-9'})
    sentinel_db = _patch_db_session(monkeypatch)

    calls = []
    from integrations.social.sync_engine import SyncEngine
    monkeypatch.setattr(SyncEngine, '_handle_sync_user',
                        staticmethod(lambda db, payload: calls.append((db, payload))))

    assert profile_sync.sync_profile('local-uuid', 42) is True
    assert len(calls) == 1                      # GATE: exactly one user persisted
    db_arg, payload = calls[0]
    assert db_arg is sentinel_db                # used the social db_session
    assert payload['user_id'] == 'local-uuid'   # keyed on the LOCAL uuid
    assert payload['central_user_id'] == '42'
    assert payload['display_name'] == 'Grace'
    assert payload['fcm_token'] == 'tok-9'


def test_sync_profile_noop_when_central_miss(monkeypatch):
    """Central not found / unreachable → no receiver call, returns False, never
    raises (best-effort)."""
    monkeypatch.setattr(profile_sync, 'fetch_central_profile', lambda cid, **k: None)
    from integrations.social.sync_engine import SyncEngine

    def _must_not_call(db, payload):
        raise AssertionError("receiver must not run when the central fetch misses")
    monkeypatch.setattr(SyncEngine, '_handle_sync_user', staticmethod(_must_not_call))

    assert profile_sync.sync_profile('local-uuid', 42) is False


def test_sync_profile_noop_on_missing_ids():
    assert profile_sync.sync_profile('', 42) is False
    assert profile_sync.sync_profile('local', None) is False


def test_sync_profile_swallows_receiver_error(monkeypatch):
    """A receiver hiccup must not propagate out to the link-device caller."""
    monkeypatch.setattr(profile_sync, 'fetch_central_profile',
                        lambda cid, **k: {'user_id': cid, 'name': 'X'})
    _patch_db_session(monkeypatch)
    from integrations.social.sync_engine import SyncEngine
    monkeypatch.setattr(SyncEngine, '_handle_sync_user',
                        staticmethod(lambda db, payload: (_ for _ in ()).throw(
                            RuntimeError('receiver down'))))
    assert profile_sync.sync_profile('local-uuid', 42) is False  # swallowed


# ── receiver round-trip: the two halves meet on a real _handle_sync_user ─────
class _FakeUser:
    """Minimal stand-in for the social User row the receiver creates/updates."""
    def __init__(self, **kw):
        self.id = kw.get('id')
        self.username = kw.get('username')
        self.display_name = kw.get('display_name')
        self.handle = kw.get('handle')
        self.role = kw.get('role')
        self.user_type = kw.get('user_type')
        self.api_token = kw.get('api_token')
        self.settings = None


class _FakeQuery:
    def __init__(self, store):
        self._store = store

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


def test_receiver_roundtrip_materialises_user_central_id_and_fcm(monkeypatch):
    """End-to-end behavioural proof: build_user_sync_payload → the REAL
    _handle_sync_user creates the local User, maps central id onto settings, and
    caches the FCM token under the LOCAL uuid (the push-path key)."""
    from integrations.social.sync_engine import SyncEngine
    import integrations.social.models as m

    # the receiver does `from .models import User` then `from .auth import
    # generate_api_token` and `from core.fcm_sync import ...` — stub those
    # boundaries so the create branch runs without a real DB / SQLite.
    monkeypatch.setattr(m, 'User', _FakeUser, raising=False)
    import integrations.social.auth as auth
    monkeypatch.setattr(auth, 'generate_api_token', lambda: 'api-tok', raising=False)

    cached = {}
    import core.fcm_sync as fcm
    monkeypatch.setattr(fcm, 'store_local_fcm_token',
                        lambda uid, tok, **k: (cached.__setitem__(uid, tok), True)[1])
    # flag_modified is ORM-only — neutralise for the stand-in row
    import sqlalchemy.orm.attributes as _attrs
    monkeypatch.setattr(_attrs, 'flag_modified', lambda obj, key: None)

    resp = {'user_id': 9003054371, 'name': 'Grace Hopper', 'FCMtoken': 'tok-G'}
    payload = profile_sync.build_user_sync_payload('local-99', 9003054371, resp)

    db = _FakeDB()
    SyncEngine._handle_sync_user(db, payload)

    row = db.store['local-99']
    assert row.username == 'Grace Hopper'
    assert row.display_name == 'Grace Hopper'
    assert row.user_type == 'human'
    assert row.settings.get('central_user_id') == '9003054371'   # #90 map
    assert cached == {'local-99': 'tok-G'}                        # keyed on LOCAL uuid
