"""Regression test for /api/social/channels/pair/verify.

Pins two bugs found while testing the iOS WhatsApp QR-pairing flow
(2026-07-20):

1. PairingManager.generate_pairing_code() never persisted the newly
   generated code, so it only existed in the one-off PairingManager
   instance created inside the /pair/generate request handler — any
   later request (a fresh PairingManager instance, per
   integrations/social/api_channels.py's per-request pattern) could
   never see it, so /pair/verify always 400'd "Invalid or expired
   pairing code". Fixed in integrations/channels/security.py by
   calling self._save_state() at the end of generate_pairing_code().

2. verify_pair_code() blind-INSERTed a new UserChannelBinding without
   checking for an existing one, so re-pairing an already-connected
   channel_type+sender_id (token expiry, retry, switching devices —
   all ordinary flows) hit the uq_user_channel_sender unique
   constraint and 500'd "Verification failed". Fixed by find-or-update,
   matching the existing pattern in create_binding() in the same file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest
from flask import Flask

from integrations.social.models import Base, get_engine
from integrations.social.api import social_bp
from integrations.social.api_channels import channel_user_bp
from integrations.social.rate_limiter import get_limiter


@pytest.fixture
def app():
    test_app = Flask(__name__)
    test_app.config['TESTING'] = True
    test_app.register_blueprint(social_bp)
    test_app.register_blueprint(channel_user_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_limiter()._buckets.clear()
    yield test_app
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(app):
    return app.test_client()


def _register(client, username='wa_tester'):
    resp = client.post('/api/social/auth/register', json={
        'username': username,
        'password': 'testpass123',
        'display_name': username.title(),
    })
    token = resp.get_json()['data']['api_token']
    return {'Authorization': f'Bearer {token}'}


def _generate_and_verify(client, headers, channel='whatsapp', sender_id=''):
    gen = client.post('/api/social/channels/pair/generate', headers=headers)
    code = gen.get_json()['data']['code']
    return client.post('/api/social/channels/pair/verify', headers=headers, json={
        'code': code, 'channel': channel, 'sender_id': sender_id,
    })


class TestPairVerifyRebind:
    def test_generate_then_verify_succeeds(self, client):
        """Bug 1: a code from /pair/generate must be accepted by the
        very next /pair/verify call (separate request handlers, each
        builds its own PairingManager instance)."""
        headers = _register(client)
        resp = _generate_and_verify(client, headers)
        body = resp.get_json()
        assert resp.status_code == 200, body
        assert body['success'] is True
        assert body['data']['channel_type'] == 'whatsapp'

    def test_repairing_same_channel_does_not_500(self, client):
        """Bug 2: verifying a second pairing code for the same
        (channel_type, sender_id) — e.g. the user reconnecting
        WhatsApp after their token expired — must update the existing
        binding, not hit the unique constraint and 500."""
        headers = _register(client)

        first = _generate_and_verify(client, headers)
        assert first.status_code == 200, first.get_json()

        second = _generate_and_verify(client, headers)
        body = second.get_json()
        assert second.status_code == 200, body
        assert body['success'] is True
        assert body['data']['channel_type'] == 'whatsapp'
        assert body['data']['is_active'] is True

    def test_invalid_code_still_rejected(self, client):
        """Fix shouldn't loosen validation — a bogus code must still 400."""
        headers = _register(client)
        resp = client.post('/api/social/channels/pair/verify', headers=headers, json={
            'code': 'NOPE00-0000', 'channel': 'whatsapp', 'sender_id': '',
        })
        body = resp.get_json()
        assert resp.status_code == 400
        assert body['success'] is False
