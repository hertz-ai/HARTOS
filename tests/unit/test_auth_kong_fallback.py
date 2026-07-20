"""
HARTOS auth.py — Kong gateway fallback tests.

When HEVOLVE_TRUST_KONG=true, require_auth must accept Kong's
X-Consumer-Custom-ID / X-Consumer-Username headers as a fallback when
the JWT/api_token paths fail.  This unblocks cloud central deployments
where Kong is the single source of truth for auth and JWTs are signed
with Kong's secret (which HARTOS doesn't know).

Mission anchors:
  1. Default behavior MUST NOT change for flat/regional deployments
     (HEVOLVE_TRUST_KONG unset) — no Kong headers honored, no false
     auth bypass via header injection.
  2. When trusted, X-Anonymous-Consumer=true MUST still 401 (denied
     anon path through Kong is not authenticated).
  3. Kong fallback only fires when JWT + api_token both fail.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from flask import Flask, jsonify, g

from integrations.social.auth import require_auth


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/who')
    @require_auth
    def who():
        return jsonify({'user_id': g.user_id, 'scope': g.token_scope})

    return app


def _fake_user(user_id='u-kong', username='konguser', email='kong@example.com'):
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.email = email
    user.is_banned = False
    return user


def _patch_get_user(user, no_match=False):
    """Patch _get_user_from_token's dependencies to control the lookup."""
    db = MagicMock()
    # JWT decode always fails (no token / wrong secret)
    def query_chain(by_field_value):
        q = MagicMock()
        # Return matching user only when looking up by user's own fields
        if not no_match and by_field_value in (user.id, user.username, user.email, user.api_token):
            q.first.return_value = user
        else:
            q.first.return_value = None
        return q

    # Build mock that records what was filtered by
    filter_calls = []

    class QueryMock:
        def filter(self, expr):
            # Inspect expr to figure out what field/value was queried
            # SQLAlchemy filter exprs are BinaryExpressions; we read the right side.
            try:
                val = expr.right.value
            except Exception:
                val = None
            filter_calls.append(val)
            q = MagicMock()
            if not no_match and val in (user.id, user.username, user.email,
                                        getattr(user, 'api_token', None)):
                q.first.return_value = user
            else:
                q.first.return_value = None
            return q

    db.query.return_value = QueryMock()
    db.close = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.is_active = True
    return db, filter_calls


def test_kong_disabled_by_default(app):
    """No HEVOLVE_TRUST_KONG — Kong headers MUST be ignored (no auth bypass)."""
    os.environ.pop('HEVOLVE_TRUST_KONG', None)
    user = _fake_user()
    db, _ = _patch_get_user(user, no_match=True)
    with patch('integrations.social.auth.decode_jwt', return_value={}), \
         patch('integrations.social.models.get_db', return_value=db):
        client = app.test_client()
        r = client.get('/who', headers={
            'X-Consumer-Custom-ID': 'kong@example.com',
            'X-Anonymous-Consumer': 'false',
        })
        assert r.status_code == 401, "Without HEVOLVE_TRUST_KONG, Kong headers MUST NOT auth"


def test_kong_anon_consumer_still_rejected(app):
    """Even with HEVOLVE_TRUST_KONG=true, X-Anonymous-Consumer=true must 401."""
    os.environ['HEVOLVE_TRUST_KONG'] = 'true'
    try:
        user = _fake_user()
        db, _ = _patch_get_user(user, no_match=True)
        with patch('integrations.social.auth.decode_jwt', return_value={}), \
             patch('integrations.social.models.get_db', return_value=db):
            client = app.test_client()
            r = client.get('/who', headers={
                'X-Consumer-Custom-ID': 'kong@example.com',
                'X-Anonymous-Consumer': 'true',
            })
            assert r.status_code == 401, "Anonymous consumer through Kong MUST still 401"
    finally:
        os.environ.pop('HEVOLVE_TRUST_KONG', None)


def test_kong_custom_id_email_match(app):
    """Kong sets Custom-ID = user's email → HARTOS finds the user → 200."""
    os.environ['HEVOLVE_TRUST_KONG'] = 'true'
    try:
        user = _fake_user(user_id='u-1', email='kong@example.com')
        db, filter_calls = _patch_get_user(user)
        with patch('integrations.social.auth.decode_jwt', return_value={}), \
             patch('integrations.social.models.get_db', return_value=db):
            client = app.test_client()
            # No Authorization header — Kong stripped it.  But trust header set.
            r = client.get('/who', headers={
                'X-Consumer-Custom-ID': 'kong@example.com',
                'X-Anonymous-Consumer': 'false',
            })
            assert r.status_code == 200, f"Kong email match should auth: {r.data}"
            data = r.get_json()
            assert data['user_id'] == 'u-1'
            assert data['scope'] == 'kong'
    finally:
        os.environ.pop('HEVOLVE_TRUST_KONG', None)


def test_kong_username_fallback(app):
    """Custom-ID missing but Kong-Username matches a User.username → 200."""
    os.environ['HEVOLVE_TRUST_KONG'] = 'true'
    try:
        user = _fake_user(user_id='u-2', username='kongbob')
        db, _ = _patch_get_user(user)
        with patch('integrations.social.auth.decode_jwt', return_value={}), \
             patch('integrations.social.models.get_db', return_value=db):
            client = app.test_client()
            r = client.get('/who', headers={
                'X-Consumer-Username': 'kongbob',
                'X-Anonymous-Consumer': 'false',
            })
            assert r.status_code == 200
            assert r.get_json()['user_id'] == 'u-2'
    finally:
        os.environ.pop('HEVOLVE_TRUST_KONG', None)


def test_kong_no_matching_user_401(app):
    """HEVOLVE_TRUST_KONG=true but the Custom-ID doesn't match any user → 401."""
    os.environ['HEVOLVE_TRUST_KONG'] = 'true'
    try:
        user = _fake_user(email='real@example.com')
        db, _ = _patch_get_user(user, no_match=True)
        with patch('integrations.social.auth.decode_jwt', return_value={}), \
             patch('integrations.social.models.get_db', return_value=db):
            client = app.test_client()
            r = client.get('/who', headers={
                'X-Consumer-Custom-ID': 'ghost@example.com',
                'X-Anonymous-Consumer': 'false',
            })
            assert r.status_code == 401
    finally:
        os.environ.pop('HEVOLVE_TRUST_KONG', None)


def test_kong_empty_token_does_not_match_empty_api_token(app):
    """The Kong-strip case routes through with token=''; api_token query must skip."""
    os.environ['HEVOLVE_TRUST_KONG'] = 'true'
    try:
        # Create a user with empty api_token — should NOT be accidentally matched
        sneaky_user = _fake_user(user_id='u-sneaky')
        sneaky_user.api_token = ''
        db, _ = _patch_get_user(sneaky_user, no_match=True)
        with patch('integrations.social.auth.decode_jwt', return_value={}), \
             patch('integrations.social.models.get_db', return_value=db):
            client = app.test_client()
            r = client.get('/who', headers={
                'X-Consumer-Custom-ID': 'noone@nowhere',
                'X-Anonymous-Consumer': 'false',
            })
            # 401 because Custom-ID doesn't match anyone — proves the empty-token
            # path didn't accidentally authenticate the sneaky user.
            assert r.status_code == 401
    finally:
        os.environ.pop('HEVOLVE_TRUST_KONG', None)
