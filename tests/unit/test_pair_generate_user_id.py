"""Regression test for the user id stored by /api/social/channels/pair/generate.

Account ids are UUID strings. generate_pair_code() used to squeeze them
through ``hash(g.user_id) % 100000``, which changes with PYTHONHASHSEED
(so the same user got a different number after every restart) and only has
100000 possible values (so two users could share one). The route now hands
the account id to PairingManager unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest
from flask import Flask

from integrations.channels.security import PairingManager
from integrations.social.models import Base, get_engine
from integrations.social.api import social_bp
from integrations.social.api_channels import channel_user_bp
from integrations.social.rate_limiter import get_limiter


@pytest.fixture
def storage_path(tmp_path, monkeypatch):
    """Point every PairingManager the route builds at a temp file."""
    path = str(tmp_path / 'pairing_data.json')
    real_init = PairingManager.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault('storage_path', path)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(PairingManager, '__init__', init)
    return path


@pytest.fixture
def app(storage_path):
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


def _register(client, username):
    resp = client.post('/api/social/auth/register', json={
        'username': username,
        'password': 'testpass123',
        'display_name': username.title(),
    })
    data = resp.get_json()['data']
    return data['id'], {'Authorization': f"Bearer {data['api_token']}"}


def _generate(client, headers):
    resp = client.post('/api/social/channels/pair/generate', headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['data']['code']


class TestPairGenerateUserId:
    def test_pending_code_keeps_the_account_id(self, client, storage_path):
        """The stored user id is the UUID itself, not a hash() of it."""
        user_id, headers = _register(client, 'wa_uuid')
        code = _generate(client, headers)

        pending = PairingManager(storage_path=storage_path)._pending_codes[code]
        assert pending.user_id == user_id

    def test_verified_session_keeps_the_account_id(self, client, storage_path):
        """The id survives verify + reload from disk, so a restarted
        process still maps the paired channel to the same account."""
        user_id, headers = _register(client, 'wa_session')
        code = _generate(client, headers)
        resp = client.post('/api/social/channels/pair/verify', headers=headers, json={
            'code': code, 'channel': 'whatsapp', 'sender_id': 'sender-1',
        })
        assert resp.status_code == 200, resp.get_json()

        reloaded = PairingManager(storage_path=storage_path)
        assert reloaded.get_user_mapping('whatsapp', 'sender-1') == (user_id, 0)
        assert [s.sender_id for s in reloaded.list_user_pairings(user_id)] == ['sender-1']

    def test_distinct_accounts_get_distinct_ids(self, client, storage_path):
        first_id, first_headers = _register(client, 'wa_first')
        second_id, second_headers = _register(client, 'wa_second')
        first_code = _generate(client, first_headers)
        second_code = _generate(client, second_headers)

        pending = PairingManager(storage_path=storage_path)._pending_codes
        assert pending[first_code].user_id == first_id
        assert pending[second_code].user_id == second_id
        assert first_id != second_id
