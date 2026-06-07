"""#90: the FCM registry is keyed by the central account id (phone, e.g.
9003054371), but the local push path carries the social User.id (a UUID).
sync_fcm_token must query the registry by the RESOLVED central id (when known)
yet cache under the LOCAL user_id the push path looks up. Behavioural: mock the
resolve/fetch/store boundaries, call the real sync_fcm_token, assert which id each
side gets. No grep tests.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.fcm_sync as fcm  # noqa: E402


def test_queries_registry_by_resolved_central_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(fcm, 'resolve_central_id', lambda uid: '9003054371')
    monkeypatch.setattr(fcm, 'fetch_central_fcm_token',
                        lambda rid, timeout=8: (seen.__setitem__('rid', rid), 'TOKEN')[1])
    stored = {}
    monkeypatch.setattr(fcm, 'store_local_fcm_token',
                        lambda uid, tok, synced_at=None: stored.update(uid=uid, tok=tok))

    out = fcm.sync_fcm_token('uuid-abc')
    assert out == 'TOKEN'
    assert seen['rid'] == '9003054371'   # queried by the CENTRAL id, not the UUID
    assert stored['uid'] == 'uuid-abc'   # cached under the LOCAL notification id


def test_falls_back_to_uuid_when_no_mapping(monkeypatch):
    seen = {}
    monkeypatch.setattr(fcm, 'resolve_central_id', lambda uid: None)
    monkeypatch.setattr(fcm, 'fetch_central_fcm_token',
                        lambda rid, timeout=8: (seen.__setitem__('rid', rid), None)[1])
    monkeypatch.setattr(fcm, 'get_local_fcm_token', lambda uid: 'CACHED')

    out = fcm.sync_fcm_token('uuid-abc')
    assert seen['rid'] == 'uuid-abc'     # unchanged behaviour: query by the UUID
    assert out == 'CACHED'               # offline miss -> last-known cached token


def test_offline_fetch_miss_returns_cached(monkeypatch):
    monkeypatch.setattr(fcm, 'resolve_central_id', lambda uid: '900')
    monkeypatch.setattr(fcm, 'fetch_central_fcm_token', lambda rid, timeout=8: None)
    monkeypatch.setattr(fcm, 'get_local_fcm_token', lambda uid: 'CACHED')
    assert fcm.sync_fcm_token('uuid-abc') == 'CACHED'
