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

from integrations.social.auth import (
    require_auth,
    require_admin,
    require_moderator,
    require_central,
    require_regional,
    hash_password,
    verify_password,
    PBKDF2_ITERATIONS,
)


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


# ─────────────────────────────────────────────────────────────────────────
# verify_password — sole login + recovery-code gate.  Dual-iteration
# (600k current / 100k legacy) PBKDF2-SHA256 loop, hmac.compare_digest
# timing-safe compare, fail-closed on malformed stored value.
# ─────────────────────────────────────────────────────────────────────────
import hashlib
import hmac as _real_hmac


def _legacy_hash(password: str, salt: str, iterations: int) -> str:
    """Reproduce the module's on-disk format for an arbitrary iteration count."""
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return f"{salt}:{h.hex()}"


class TestVerifyPassword:
    def test_hash_password_format_is_salt_colon_hex(self):
        """hash_password emits salt:hex with a 600k-derived digest."""
        stored = hash_password('correct horse battery staple')
        assert stored.count(':') == 1
        salt, digest = stored.split(':', 1)
        # 16 bytes token_hex → 32 hex chars salt; sha256 digest → 64 hex chars.
        assert len(salt) == 32
        assert len(digest) == 64
        int(salt, 16)   # salt is valid hex (would raise otherwise)
        int(digest, 16)  # digest is valid hex
        # The stored digest must equal a fresh 600k derivation of the same input.
        assert digest == hashlib.pbkdf2_hmac(
            'sha256', b'correct horse battery staple', salt.encode(),
            PBKDF2_ITERATIONS).hex()

    def test_roundtrip_correct_password(self):
        stored = hash_password('s3cr3t-pw')
        assert verify_password('s3cr3t-pw', stored) is True

    def test_roundtrip_uses_random_salt_each_time(self):
        """Two hashes of the same password differ (salted); both verify."""
        a = hash_password('same-pw')
        b = hash_password('same-pw')
        assert a != b
        assert verify_password('same-pw', a) is True
        assert verify_password('same-pw', b) is True

    def test_wrong_password_fails(self):
        stored = hash_password('right-pw')
        assert verify_password('wrong-pw', stored) is False

    def test_wrong_password_off_by_one_char_fails(self):
        stored = hash_password('password123')
        assert verify_password('password124', stored) is False

    def test_none_stored_fails_closed(self):
        assert verify_password('anything', None) is False

    def test_empty_stored_fails_closed(self):
        assert verify_password('anything', '') is False

    def test_stored_without_colon_fails_closed(self):
        assert verify_password('anything', 'no-delimiter-here') is False

    def test_empty_hash_component_fails_closed(self):
        """'salt:' → empty digest must never match anything."""
        assert verify_password('anything', 'deadbeef:') is False

    def test_bare_colon_stored_fails_closed_no_crash(self):
        """':' → salt='' and digest='' — must return False, not raise."""
        assert verify_password('anything', ':') is False

    def test_non_hex_digest_fails_closed(self):
        """A malformed (non-hex) stored digest fails closed, no exception."""
        salt = 'aa' * 16
        assert verify_password('anything', f'{salt}:zzzznothexzzzz') is False

    def test_extra_colon_in_stored_fails(self):
        """split(':', 1) keeps everything after the first colon as the digest;
        a value with an extra colon can't be a valid hex digest → fails."""
        salt = 'bb' * 16
        assert verify_password('pw', f'{salt}:aa:bb') is False

    def test_legacy_100k_iteration_hash_verifies(self):
        """A password stored under the OLD 100k count must still log in
        (proves the dual-iteration loop's second branch)."""
        salt = secrets_hex()
        stored = _legacy_hash('legacy-user-pw', salt, 100_000)
        assert verify_password('legacy-user-pw', stored) is True

    def test_current_600k_iteration_hash_verifies(self):
        salt = secrets_hex()
        stored = _legacy_hash('modern-user-pw', salt, PBKDF2_ITERATIONS)
        assert verify_password('modern-user-pw', stored) is True

    def test_intermediate_iteration_count_is_rejected(self):
        """A digest derived with an UNSUPPORTED iteration count (e.g. 200k)
        must NOT verify — only 600k and 100k are accepted."""
        salt = secrets_hex()
        stored = _legacy_hash('some-pw', salt, 200_000)
        assert verify_password('some-pw', stored) is False

    def test_correct_password_wrong_salt_fails(self):
        """Right password, but the stored digest was derived under a
        different salt → must fail (salt is part of the secret)."""
        good = hash_password('shared-pw')
        salt_a, digest_a = good.split(':', 1)
        other_salt = ('cc' * 16)
        assert other_salt != salt_a
        assert verify_password('shared-pw', f'{other_salt}:{digest_a}') is False

    def test_empty_password_matches_its_own_hash(self):
        stored = hash_password('')
        assert verify_password('', stored) is True
        assert verify_password('x', stored) is False

    def test_unicode_password_roundtrip(self):
        pw = 'pÄsswörd-日本語-🔐'
        stored = hash_password(pw)
        assert verify_password(pw, stored) is True
        assert verify_password('pÄsswörd-日本語', stored) is False

    def test_uses_timing_safe_compare(self):
        """Security invariant: the digest comparison goes through
        hmac.compare_digest (constant-time), not a plain '=='.  Spy on the
        real primitive and confirm it is exercised while the correct
        password still verifies."""
        stored = hash_password('timing-pw')
        with patch('integrations.social.auth.hmac.compare_digest',
                   wraps=_real_hmac.compare_digest) as spy:
            assert verify_password('timing-pw', stored) is True
        assert spy.called, "verify_password must use hmac.compare_digest"

    def test_wrong_password_still_runs_both_iteration_branches(self):
        """A wrong password with a well-formed stored value exhausts both
        iteration counts (600k then 100k) before failing — proves the loop
        doesn't short-circuit incorrectly."""
        stored = hash_password('right')
        with patch('integrations.social.auth.hmac.compare_digest',
                   wraps=_real_hmac.compare_digest) as spy:
            assert verify_password('wrong', stored) is False
        # Both branches attempted → compare_digest invoked twice.
        assert spy.call_count == 2


def secrets_hex() -> str:
    """A 32-hex-char salt matching hash_password's token_hex(16) shape."""
    import secrets
    return secrets.token_hex(16)


# ─────────────────────────────────────────────────────────────────────────
# Role gates — require_admin / require_moderator / require_central /
# require_regional.  Behavioral 200-vs-403 assertions (previously only
# AST source-shape guards existed).  These gates wrap require_auth, so we
# patch _get_user_from_token to inject an authenticated user with a chosen
# role + flag set, then assert the HTTP status the gate produces.
# ─────────────────────────────────────────────────────────────────────────

def _fake_db():
    """A DB mock satisfying require_auth's commit/close/rollback lifecycle."""
    db = MagicMock()
    db.is_active = True
    return db


def _role_user(role='flat', is_admin=False, is_moderator=False, user_id='u-role'):
    """Authenticated user with explicit role + boolean flags.

    is_admin / is_moderator MUST be set explicitly: a bare MagicMock
    attribute is truthy, which would silently grant every gate.
    """
    u = MagicMock()
    u.id = user_id
    u.role = role
    u.is_admin = is_admin
    u.is_moderator = is_moderator
    u.is_banned = False
    return u


@pytest.fixture
def role_app():
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/admin')
    @require_admin
    def admin_route():
        return jsonify({'ok': True, 'gate': 'admin'})

    @app.route('/moderator')
    @require_moderator
    def moderator_route():
        return jsonify({'ok': True, 'gate': 'moderator'})

    @app.route('/central')
    @require_central
    def central_route():
        return jsonify({'ok': True, 'gate': 'central'})

    @app.route('/regional')
    @require_regional
    def regional_route():
        return jsonify({'ok': True, 'gate': 'regional'})

    return app


@pytest.fixture(autouse=True)
def _clean_role_env():
    """Role gates must not be perturbed by cloud/kong env from other tests."""
    saved = {k: os.environ.get(k) for k in
             ('HEVOLVE_CLOUD_MODE', 'HEVOLVE_TRUST_KONG')}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _call_gate(app, path, user):
    """Drive `path` with an authenticated `user` injected via the patched
    token lookup.  Returns the Flask test response."""
    db = _fake_db()
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(user, db)):
        client = app.test_client()
        return client.get(path, headers={'Authorization': 'Bearer faketoken'})


class TestRequireAdmin:
    def test_is_admin_flag_allows(self, role_app):
        r = _call_gate(role_app, '/admin', _role_user(role='flat', is_admin=True))
        assert r.status_code == 200
        assert r.get_json()['gate'] == 'admin'

    def test_central_role_allows(self, role_app):
        r = _call_gate(role_app, '/admin', _role_user(role='central'))
        assert r.status_code == 200

    def test_flat_user_denied_403(self, role_app):
        r = _call_gate(role_app, '/admin', _role_user(role='flat'))
        assert r.status_code == 403
        assert r.get_json()['error'] == 'Admin access required'

    def test_regional_role_denied_403(self, role_app):
        """regional is below admin — must not pass the admin gate."""
        r = _call_gate(role_app, '/admin', _role_user(role='regional'))
        assert r.status_code == 403

    def test_moderator_flag_alone_denied_403(self, role_app):
        """is_moderator does NOT grant admin."""
        r = _call_gate(role_app, '/admin',
                       _role_user(role='flat', is_moderator=True))
        assert r.status_code == 403

    def test_none_role_defaults_flat_and_denied(self, role_app):
        """user.role=None → 'flat' fallback → denied (exercises `or 'flat'`)."""
        r = _call_gate(role_app, '/admin', _role_user(role=None))
        assert r.status_code == 403

    def test_unauthenticated_401_before_role_check(self, role_app):
        """No user from token → require_auth 401s before the role check."""
        db = _fake_db()
        with patch('integrations.social.auth._get_user_from_token',
                   return_value=(None, db)):
            client = role_app.test_client()
            r = client.get('/admin', headers={'Authorization': 'Bearer x'})
        assert r.status_code == 401

    def test_missing_auth_header_401(self, role_app):
        client = role_app.test_client()
        r = client.get('/admin')
        assert r.status_code == 401


class TestRequireModerator:
    def test_regional_role_allows(self, role_app):
        r = _call_gate(role_app, '/moderator', _role_user(role='regional'))
        assert r.status_code == 200

    def test_central_role_allows(self, role_app):
        r = _call_gate(role_app, '/moderator', _role_user(role='central'))
        assert r.status_code == 200

    def test_is_moderator_flag_allows(self, role_app):
        r = _call_gate(role_app, '/moderator',
                       _role_user(role='flat', is_moderator=True))
        assert r.status_code == 200

    def test_is_admin_flag_allows(self, role_app):
        r = _call_gate(role_app, '/moderator',
                       _role_user(role='flat', is_admin=True))
        assert r.status_code == 200

    def test_flat_user_denied_403(self, role_app):
        r = _call_gate(role_app, '/moderator', _role_user(role='flat'))
        assert r.status_code == 403
        assert r.get_json()['error'] == 'Moderator access required'


class TestRequireCentral:
    def test_central_role_allows(self, role_app):
        r = _call_gate(role_app, '/central', _role_user(role='central'))
        assert r.status_code == 200

    def test_is_admin_flag_allows(self, role_app):
        r = _call_gate(role_app, '/central',
                       _role_user(role='flat', is_admin=True))
        assert r.status_code == 200

    def test_regional_role_denied_403(self, role_app):
        """regional must NOT reach central."""
        r = _call_gate(role_app, '/central', _role_user(role='regional'))
        assert r.status_code == 403
        assert r.get_json()['error'] == 'Central access required'

    def test_moderator_flag_alone_denied_403(self, role_app):
        """is_moderator does not grant central (only is_admin or central role)."""
        r = _call_gate(role_app, '/central',
                       _role_user(role='flat', is_moderator=True))
        assert r.status_code == 403

    def test_flat_user_denied_403(self, role_app):
        r = _call_gate(role_app, '/central', _role_user(role='flat'))
        assert r.status_code == 403


class TestRequireRegional:
    def test_regional_role_allows(self, role_app):
        r = _call_gate(role_app, '/regional', _role_user(role='regional'))
        assert r.status_code == 200

    def test_central_role_allows(self, role_app):
        r = _call_gate(role_app, '/regional', _role_user(role='central'))
        assert r.status_code == 200

    def test_is_admin_flag_allows(self, role_app):
        r = _call_gate(role_app, '/regional',
                       _role_user(role='flat', is_admin=True))
        assert r.status_code == 200

    def test_is_moderator_flag_allows(self, role_app):
        r = _call_gate(role_app, '/regional',
                       _role_user(role='flat', is_moderator=True))
        assert r.status_code == 200

    def test_flat_user_denied_403(self, role_app):
        r = _call_gate(role_app, '/regional', _role_user(role='flat'))
        assert r.status_code == 403
        assert r.get_json()['error'] == 'Regional access required'
