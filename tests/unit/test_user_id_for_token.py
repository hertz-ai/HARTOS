"""user_id_for_token: the one public answer to "whose token is this?".

Measured live 2026-09-25 on the owner's desktop: after a cloud OTP login the
window holds the Kong-issued cloud token (32 chars, opaque, no dots), and the
login sync stores it as the user's api_token. The canonical resolver accepts
it (GET /api/social/auth/me -> 200, user 10202), but Nunba's /agents/sync,
/api/chat-sync and SSE stream each decoded the token as a JWT themselves, so
the same token got 401 "Authentication required" -> the amber "Session
expired" toast and "Please login to talk to agent". Those callers now ask
this function, which is the canonical resolver's answer.
"""
from unittest.mock import MagicMock, patch

import integrations.social.auth as auth


def _db_returning(user):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    return db


def _user(uid='10202', banned=False):
    u = MagicMock()
    u.id = uid
    u.is_banned = banned
    return u


class TestUserIdForToken:

    def test_an_opaque_cloud_token_resolves_through_the_stored_api_token(self):
        db = _db_returning(_user('10202'))
        with patch('integrations.social.models.get_db', return_value=db):
            assert auth.user_id_for_token('a' * 32) == '10202'
        db.close.assert_called()

    def test_a_local_jwt_resolves(self):
        token = auth.generate_jwt('u-local', 'someone', 'flat')
        db = _db_returning(_user('u-local'))
        with patch('integrations.social.models.get_db', return_value=db):
            assert auth.user_id_for_token(token) == 'u-local'

    def test_an_unknown_token_is_none_and_the_session_is_closed(self):
        db = _db_returning(None)
        with patch('integrations.social.models.get_db', return_value=db):
            assert auth.user_id_for_token('not-a-known-token') is None
        db.close.assert_called()

    def test_a_banned_user_is_none(self):
        db = _db_returning(_user('10202', banned=True))
        with patch('integrations.social.models.get_db', return_value=db):
            assert auth.user_id_for_token('b' * 32) is None

    def test_an_empty_token_is_none_without_touching_the_db(self):
        with patch('integrations.social.models.get_db') as get_db:
            assert auth.user_id_for_token('') is None
            assert auth.user_id_for_token(None) is None
        get_db.assert_not_called()
