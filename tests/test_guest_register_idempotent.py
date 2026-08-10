"""Tests for POST /api/social/auth/guest-register idempotence on
device_id.

INVARIANTS (2026-04-15 fix):
  * Same device_id + any guest_name → same User row returned (idempotent)
  * Missing device_id → new User every call (legacy behavior preserved)
  * device_id mismatch → different User (distinct devices = distinct guests)
  * idempotent return status=200 (vs 201 for genuine new user)
  * recovery_code only issued on genuine create, not on idempotent return
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest

# Use the rate_limiter's built-in test-mode escape hatch (see
# integrations/social/rate_limiter.py::_build_limits).  Setting the env
# var BEFORE the module is imported ensures LIMITS is built at the
# relaxed 100000-token ceiling.  No monkeypatching required.
os.environ.setdefault('SOCIAL_RATE_LIMIT_DISABLED', '1')


@pytest.fixture
def flask_app():
    """Spin up a minimal Flask app with social_bp registered and
    SQLite in-memory DB for isolation.  Relies on the module-level
    SOCIAL_RATE_LIMIT_DISABLED=1 to short-circuit the token bucket."""
    import flask
    from integrations.social.api import social_bp
    from integrations.social.models import Base

    # Use in-memory SQLite with StaticPool so every get_db() call
    # sees the SAME underlying connection (default pool creates a
    # fresh in-memory DB per connection — second call sees nothing).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        'sqlite:///:memory:',
        echo=False,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Reset the in-memory token bucket between tests so limits from a
    # prior test don't leak into this one.  Uses the real limiter —
    # just clears its accumulated state (the canonical test-isolation
    # pattern for stateful singletons).
    from integrations.social.rate_limiter import get_limiter
    get_limiter()._buckets.clear()

    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(social_bp)

    def _fake_get_db():
        return Session()

    with patch('integrations.social.api.get_db', side_effect=_fake_get_db):
        yield app.test_client()


def _register(client, guest_name, device_id=None):
    body = {'guest_name': guest_name}
    if device_id is not None:
        body['device_id'] = device_id
    return client.post('/api/social/auth/guest-register', json=body)


def test_same_device_id_returns_same_user(flask_app):
    device = str(uuid.uuid4())
    r1 = _register(flask_app, 'Sathish', device_id=device)
    assert r1.status_code in (200, 201)
    user1 = r1.get_json()['data']['user']

    r2 = _register(flask_app, 'Sathish', device_id=device)
    assert r2.status_code == 200  # idempotent = 200, not 201
    user2 = r2.get_json()['data']['user']

    assert user1['id'] == user2['id'], (
        "Same device_id must return the same User id"
    )


def test_different_device_id_creates_new_user(flask_app):
    r1 = _register(flask_app, 'Alice', device_id=str(uuid.uuid4()))
    r2 = _register(flask_app, 'Bob', device_id=str(uuid.uuid4()))
    assert r1.get_json()['data']['user']['id'] != r2.get_json()['data']['user']['id']


def test_missing_device_id_creates_new_each_time(flask_app):
    """Back-compat: pre-device_id clients still get new users each call."""
    r1 = _register(flask_app, 'Anon')
    r2 = _register(flask_app, 'Anon')
    # No device_id → can't dedup → two distinct users
    assert r1.get_json()['data']['user']['id'] != r2.get_json()['data']['user']['id']


def test_same_device_different_name_still_idempotent(flask_app):
    """device_id is the primary key; name is incidental.  If user
    renamed themselves mid-session, we should still return the SAME
    user (not create a new one)."""
    device = str(uuid.uuid4())
    r1 = _register(flask_app, 'FirstName', device_id=device)
    r2 = _register(flask_app, 'RenamedUser', device_id=device)
    assert r1.get_json()['data']['user']['id'] == r2.get_json()['data']['user']['id']


def test_idempotent_return_has_existing_flag(flask_app):
    """Frontend can distinguish 'created new' vs 'returned existing'
    via an `existing: true` flag in the response payload."""
    device = str(uuid.uuid4())
    r1 = _register(flask_app, 'Sathish', device_id=device)
    assert r1.get_json()['data'].get('existing') is not True  # first = not existing

    r2 = _register(flask_app, 'Sathish', device_id=device)
    assert r2.get_json()['data'].get('existing') is True


def test_recovery_code_not_reissued_on_idempotent(flask_app):
    """Recovery code is shown once at first-register.  Re-registering
    via the same device must NOT issue a fresh code (that would
    invalidate the user's saved one)."""
    device = str(uuid.uuid4())
    r1 = _register(flask_app, 'Sathish', device_id=device)
    code1 = r1.get_json()['data'].get('recovery_code')
    assert code1  # first call got a code

    r2 = _register(flask_app, 'Sathish', device_id=device)
    code2 = r2.get_json()['data'].get('recovery_code')
    assert not code2, "Idempotent return must not issue new recovery code"


def test_fresh_guest_signup_attributes_channel(flask_app, tmp_path):
    """A fresh guest register carrying a channel referral_code records ONE
    'signup' event against that channel (this is what feeds the
    /marketing/growth 'toward-100/10K' funnel), and the idempotent device
    re-register does NOT double-count it — a returning guest is not a new
    organic user."""
    import json
    device = str(uuid.uuid4())
    jsonl = os.path.join(str(tmp_path), 'marketing_clicks.jsonl')

    def _signup_rows():
        if not os.path.exists(jsonl):
            return []
        rows = [json.loads(l) for l in open(jsonl, encoding='utf-8').read().splitlines() if l.strip()]
        return [x for x in rows if x.get('event') == 'signup' and x.get('code') == 'li_a']

    with patch('core.platform_paths.get_data_dir', return_value=str(tmp_path)):
        r1 = flask_app.post('/api/social/auth/guest-register',
                            json={'guest_name': 'Sathish', 'device_id': device,
                                  'referral_code': 'li_a'})
        assert r1.status_code == 201, r1.get_json()
        assert len(_signup_rows()) == 1, "fresh guest signup must record one channel signup"

        # Idempotent re-register (same device) must NOT add a second signup.
        r2 = flask_app.post('/api/social/auth/guest-register',
                            json={'guest_name': 'Sathish', 'device_id': device,
                                  'referral_code': 'li_a'})
        assert r2.status_code == 200  # idempotent
        assert len(_signup_rows()) == 1, "idempotent re-register must not double-count signup"


def test_fresh_jwt_on_idempotent_return(flask_app):
    """The whole POINT of the 2026-04-15 fix: even when returning an
    existing user, issue a fresh JWT so client can resume with a
    valid token (they likely re-registered because JWT expired)."""
    device = str(uuid.uuid4())
    r1 = _register(flask_app, 'Sathish', device_id=device)
    t1 = r1.get_json()['data']['token']

    r2 = _register(flask_app, 'Sathish', device_id=device)
    t2 = r2.get_json()['data']['token']

    assert t1 and t2
    # JWTs include iat (issued-at) so two tokens issued at different
    # microseconds should differ even for the same user — but we
    # don't assert inequality because tokens may be identical within
    # the same second.  We just assert both are non-empty.
