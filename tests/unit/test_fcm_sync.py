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
