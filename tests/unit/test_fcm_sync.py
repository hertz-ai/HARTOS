"""FCM token sync — pull the centrally-registered token into local SQLite so a
node can push without a crossbar relay (core/fcm_sync.py).

Behavioural tests: the pure parser over real registry shapes; the fetch with a
mocked HTTP boundary; the sync flow with mocked fetch/store/get; and a real
in-memory SQLite round-trip for the store/get SQL.  No grep tests.
"""
from __future__ import annotations

import contextlib
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import fcm_sync


# ── pure parser ─────────────────────────────────────────────────────────────
def test_parse_fcm_token_response_variants():
    assert fcm_sync.parse_fcm_token_response({'fcm_token': 'abc'}) == 'abc'
    assert fcm_sync.parse_fcm_token_response({'token': 'xyz'}) == 'xyz'
    # the real "unregistered" shape the live registry returned for 9003054371
    assert fcm_sync.parse_fcm_token_response({'detail': 'user not Found'}) is None
    assert fcm_sync.parse_fcm_token_response({'fcm_token': '   '}) is None
    assert fcm_sync.parse_fcm_token_response({}) is None
    assert fcm_sync.parse_fcm_token_response(None) is None
    assert fcm_sync.parse_fcm_token_response('notadict') is None


# ── fetch (mocked HTTP boundary) ────────────────────────────────────────────
def _mock_requests(status, body):
    class _Resp:
        status_code = status
        def json(self):
            return body
    return types.SimpleNamespace(get=lambda *a, **k: _Resp())


def test_fetch_returns_token_on_200(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', _mock_requests(200, {'fcm_token': 'tok123'}))
    assert fcm_sync.fetch_central_fcm_token('9003054371') == 'tok123'


def test_fetch_returns_none_on_404_user_not_found(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', _mock_requests(404, {'detail': 'user not Found'}))
    assert fcm_sync.fetch_central_fcm_token('9003054371') is None


def test_fetch_none_without_user_id():
    assert fcm_sync.fetch_central_fcm_token('') is None
    assert fcm_sync.fetch_central_fcm_token(None) is None


# ── sync flow (mocked fetch/store/get boundaries) ───────────────────────────
def test_sync_stores_and_returns_fresh_token(monkeypatch):
    stored = {}
    monkeypatch.setattr(fcm_sync, 'fetch_central_fcm_token', lambda uid, **k: 'fresh')
    monkeypatch.setattr(fcm_sync, 'store_local_fcm_token',
                        lambda uid, tok, **k: (stored.__setitem__(uid, tok), True)[1])
    assert fcm_sync.sync_fcm_token('u1', synced_at='2026-06-04') == 'fresh'
    assert stored == {'u1': 'fresh'}


def test_sync_falls_back_to_local_on_miss(monkeypatch):
    monkeypatch.setattr(fcm_sync, 'fetch_central_fcm_token', lambda uid, **k: None)
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: 'cached_old')

    def _no_store(*a, **k):
        raise AssertionError("must not store when the central fetch misses")
    monkeypatch.setattr(fcm_sync, 'store_local_fcm_token', _no_store)
    assert fcm_sync.sync_fcm_token('u1') == 'cached_old'


# ── #90: query the registry by the CENTRAL id when the mapping is known ─────
def test_sync_queries_by_central_id_when_mapped(monkeypatch):
    """A local UUID with a central-id mapping → the registry is queried by the
    CENTRAL id (what it's keyed with), but the token is cached under the LOCAL
    uuid (what the push path looks up)."""
    seen = {}
    monkeypatch.setattr(fcm_sync, 'resolve_central_id', lambda uid: '9003054371')
    monkeypatch.setattr(fcm_sync, 'fetch_central_fcm_token',
                        lambda rid, **k: (seen.__setitem__('queried', rid), 'tok')[1])
    monkeypatch.setattr(fcm_sync, 'store_local_fcm_token',
                        lambda uid, tok, **k: (seen.__setitem__('cached', (uid, tok)), True)[1])
    assert fcm_sync.sync_fcm_token('local-uuid') == 'tok'
    assert seen['queried'] == '9003054371'           # queried by central id
    assert seen['cached'] == ('local-uuid', 'tok')   # cached under local uuid


def test_sync_queries_by_uuid_when_no_mapping(monkeypatch):
    """No mapping → query by the uuid itself — byte-for-byte the pre-#90
    behaviour (zero regression)."""
    seen = {}
    monkeypatch.setattr(fcm_sync, 'resolve_central_id', lambda uid: None)
    monkeypatch.setattr(fcm_sync, 'fetch_central_fcm_token',
                        lambda rid, **k: (seen.__setitem__('queried', rid), None)[1])
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: 'cached')
    assert fcm_sync.sync_fcm_token('local-uuid') == 'cached'
    assert seen['queried'] == 'local-uuid'


def _fake_social_models(user_obj):
    """A stand-in integrations.social.models exposing db_session + User, where
    a User query resolves to ``user_obj`` (or None)."""
    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return user_obj

    class _DB:
        def query(self, _model):
            return _Query()

    @contextlib.contextmanager
    def db_session(commit=True):
        yield _DB()

    return types.SimpleNamespace(db_session=db_session, User=types.SimpleNamespace(id=None))


def test_resolve_central_id_reads_user_settings(monkeypatch):
    monkeypatch.setitem(sys.modules, 'integrations.social.models',
                        _fake_social_models(types.SimpleNamespace(
                            settings={'central_user_id': '9003054371'})))
    assert fcm_sync.resolve_central_id('uuid') == '9003054371'


def test_resolve_central_id_none_when_unset_or_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, 'integrations.social.models',
                        _fake_social_models(types.SimpleNamespace(settings={})))
    assert fcm_sync.resolve_central_id('uuid') is None
    monkeypatch.setitem(sys.modules, 'integrations.social.models',
                        _fake_social_models(None))  # user not found
    assert fcm_sync.resolve_central_id('uuid') is None
    assert fcm_sync.resolve_central_id('') is None  # no user_id


def test_set_central_id_writes_user_settings(monkeypatch):
    user = types.SimpleNamespace(settings={})
    monkeypatch.setitem(sys.modules, 'integrations.social.models',
                        _fake_social_models(user))
    import sqlalchemy.orm.attributes as _attrs
    monkeypatch.setattr(_attrs, 'flag_modified', lambda obj, key: None)  # ORM-only
    assert fcm_sync.set_central_id('uuid', '9003054371') is True
    assert user.settings.get('central_user_id') == '9003054371'
    assert fcm_sync.set_central_id('uuid', '') is False  # nothing to store


# ── store/get round-trip against a real in-memory SQLite ────────────────────
def test_store_and_get_roundtrip(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine('sqlite:///:memory:',
                           connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def fake_db_session(commit=True):
        s = Session()
        try:
            yield s
            if commit:
                s.commit()
        finally:
            s.close()

    # fcm_sync lazily does `from integrations.social.models import db_session`;
    # patch the attribute it will import.
    import integrations.social.models as m
    monkeypatch.setattr(m, 'db_session', fake_db_session, raising=False)

    assert fcm_sync.store_local_fcm_token('9003054371', 'tokABC', synced_at='2026') is True
    assert fcm_sync.get_local_fcm_token('9003054371') == 'tokABC'
    # upsert: a second sync overwrites, doesn't duplicate
    assert fcm_sync.store_local_fcm_token('9003054371', 'tokNEW', synced_at='2026') is True
    assert fcm_sync.get_local_fcm_token('9003054371') == 'tokNEW'
    assert fcm_sync.get_local_fcm_token('unregistered') is None


# ── send path: message build + privacy notice + credential-gated send ───────
def test_build_fcm_v1_message_stamps_privacy_notice():
    msg = fcm_sync.build_fcm_v1_message('tok', 'Title', 'Body',
                                        data={'k': 1, 'topic': 'chat.social'})
    m = msg['message']
    assert m['token'] == 'tok'
    assert m['notification'] == {'title': 'Title', 'body': 'Body'}
    assert m['data']['k'] == '1'                     # v1 requires string values
    assert m['data']['topic'] == 'chat.social'
    assert m['data']['privacy_tier_skipped'] == 'true'
    assert 'local-private tier' in m['data']['privacy_notice']


def test_build_fcm_v1_message_no_notice_when_disabled():
    msg = fcm_sync.build_fcm_v1_message('tok', '', '', privacy_tier_skipped=False)
    assert 'privacy_notice' not in msg['message']['data']
    assert 'notification' not in msg['message']      # no title/body → no notif block


def test_send_no_op_without_credential(monkeypatch):
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: 'tok')
    monkeypatch.delenv('HART_FCM_ACCESS_TOKEN', raising=False)
    monkeypatch.delenv('HART_FCM_SA_FILE', raising=False)
    monkeypatch.delenv('HART_FCM_PROJECT', raising=False)

    def _no_post(*a, **k):
        raise AssertionError("must not POST to FCM without a credential")
    monkeypatch.setitem(sys.modules, 'requests', types.SimpleNamespace(post=_no_post))
    assert fcm_sync.send_fcm_push('u1', 't', 'b') is False


def test_send_none_without_token(monkeypatch):
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: None)
    monkeypatch.setattr(fcm_sync, 'sync_fcm_token', lambda uid, **k: None)
    assert fcm_sync.send_fcm_push('u1', 't', 'b') is False


def test_send_posts_to_fcm_with_credential(monkeypatch):
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: 'tokXYZ')
    monkeypatch.setenv('HART_FCM_ACCESS_TOKEN', 'access123')
    monkeypatch.setenv('HART_FCM_PROJECT', 'hart-proj')
    captured = {}

    class _Resp:
        status_code = 200
        text = ''

    def _post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()
    monkeypatch.setitem(sys.modules, 'requests', types.SimpleNamespace(post=_post))

    assert fcm_sync.send_fcm_push('9003054371', 'Consent', 'Approve external posting?') is True
    assert 'hart-proj' in captured['url'] and captured['url'].endswith('/messages:send')
    assert captured['headers']['Authorization'] == 'Bearer access123'
    m = captured['json']['message']
    assert m['token'] == 'tokXYZ'
    assert m['notification'] == {'title': 'Consent', 'body': 'Approve external posting?'}
    assert m['data']['privacy_tier_skipped'] == 'true'        # the tier-skip notice rides along
    assert 'central FCM relay' in m['data']['privacy_notice']


def test_send_skips_network_sync_when_no_credential(monkeypatch):
    """The cheap credential gate must short-circuit BEFORE the token network
    sync. A credential-less node (the default today) must never fire the up-to-8s
    blocking registry GET — which send_fcm_push is called with per expired message
    in DeliveryTracker's cleanup loop (2026-06-05 sweep, governor-hang family)."""
    monkeypatch.delenv('HART_FCM_ACCESS_TOKEN', raising=False)
    monkeypatch.delenv('HART_FCM_SA_FILE', raising=False)
    monkeypatch.delenv('HART_FCM_PROJECT', raising=False)
    monkeypatch.setattr(fcm_sync, 'get_local_fcm_token', lambda uid: None)   # cache miss

    def _no_sync(*a, **k):
        raise AssertionError(
            "must not hit the central FCM registry without a push credential")
    monkeypatch.setattr(fcm_sync, 'sync_fcm_token', _no_sync)
    assert fcm_sync.send_fcm_push('u1', 't', 'b') is False
