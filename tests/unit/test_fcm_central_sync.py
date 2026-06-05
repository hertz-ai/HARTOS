"""FCM token flows central -> local at user-sync, keyed by the social UUID (#90).

The bug: the push path (core/peer_link/local_subscribers -> send_fcm_push) looks
the token up by the notification user_id (a social UUID), but the central FCM
registry keys by account-number/phone, so the lazy HARTOS->central pull-by-UUID
always missed -> no token -> no push.

The fix (central -> local, the documented design): when central syncs a user
DOWN via /auth/sync-user -> SyncEngine._handle_sync_user, it may include the FCM
token it owns; HARTOS caches it locally keyed by the SAME UUID. The token
arrives already mapped to the local UUID, so send_fcm_push -> get_local_fcm_token
(uuid) resolves it with no identity-mapping needed.

These behavioural tests drive the REAL _handle_sync_user against an in-memory DB
and assert the token round-trips into the local fcm cache. No grep tests.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def social_db(monkeypatch):
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import integrations.social.models as M
    except Exception as e:  # pragma: no cover
        pytest.skip(f"social models / sqlalchemy unavailable: {e}")

    engine = create_engine('sqlite:///:memory:')          # SingletonThreadPool → shared
    M.User.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def _fake_db_session(commit=True):
        s = Session()
        try:
            yield s
            if commit:
                s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(M, 'db_session', _fake_db_session)
    return M


def _sync(M, payload):
    from integrations.social.sync_engine import SyncEngine
    with M.db_session() as db:
        SyncEngine._handle_sync_user(db, payload)


def test_sync_caches_fcm_token_keyed_by_uuid(social_db):
    from core.fcm_sync import get_local_fcm_token
    _sync(social_db, {
        'user_id': 'uuid-alice', 'username': 'alice', 'fcm_token': 'dev-token-xyz',
    })
    # The push path reads by the SAME UUID the notification topic uses:
    assert get_local_fcm_token('uuid-alice') == 'dev-token-xyz'


def test_sync_accepts_central_FCMtoken_field_alias(social_db):
    from core.fcm_sync import get_local_fcm_token
    # Central (Hevolve_Database) owns the column as `User.FCMtoken`; accept that
    # casing too so the sync payload can pass it through verbatim.
    _sync(social_db, {
        'user_id': 'uuid-bob', 'username': 'bob', 'FCMtoken': 'dev-token-bob',
    })
    assert get_local_fcm_token('uuid-bob') == 'dev-token-bob'


def test_sync_without_token_is_a_clean_noop(social_db):
    from core.fcm_sync import get_local_fcm_token
    # Older central nodes omit the token — the user still syncs, no token cached,
    # no crash.
    _sync(social_db, {'user_id': 'uuid-carol', 'username': 'carol'})
    assert get_local_fcm_token('uuid-carol') is None
    # ...and the user record itself was still created.
    with social_db.db_session(commit=False) as db:
        assert db.query(social_db.User).filter_by(id='uuid-carol').first() is not None


def test_resync_updates_the_cached_token(social_db):
    """A device re-registers → central re-syncs → local cache reflects the new
    token (upsert keyed by UUID, not a duplicate)."""
    from core.fcm_sync import get_local_fcm_token
    _sync(social_db, {'user_id': 'uuid-dan', 'username': 'dan', 'fcm_token': 'old'})
    _sync(social_db, {'user_id': 'uuid-dan', 'username': 'dan', 'fcm_token': 'new'})
    assert get_local_fcm_token('uuid-dan') == 'new'
