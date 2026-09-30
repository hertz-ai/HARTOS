"""voice_profile is retired: an agent's voice is its avatar's recorded voice
(core/teacher_avatar.py), so a per-agent voice profile has no reader.

The v37 column on ``users`` stays until every node has stopped writing it
(tests/unit/test_voice_profile_migration.py still covers that migration); it
is dropped in a later release.  This file guards the API side: the field is
neither stored nor echoed, and a client that still sends it is not refused.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from flask import Flask  # noqa: E402

from integrations.social.api import social_bp  # noqa: E402
from integrations.social.models import Base, User, get_engine  # noqa: E402
from integrations.social.rate_limiter import get_limiter  # noqa: E402


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(social_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_limiter()._buckets.clear()
    yield app.test_client()
    Base.metadata.drop_all(engine)


def _register(client, username):
    resp = client.post('/api/social/auth/register', json={
        'username': username, 'password': 'testpass123',
        'display_name': username.title()})
    data = resp.get_json()['data']
    return data['id'], {'Authorization': f"Bearer {data['api_token']}"}


def test_the_model_has_no_voice_profile():
    assert not hasattr(User, 'voice_profile')
    assert 'voice_profile' not in User(id='u', username='u').to_dict()


def test_a_body_voice_profile_is_ignored_not_refused(client):
    user_id, auth = _register(client, 'owner_one')
    resp = client.post(f'/api/social/users/{user_id}/agents', headers=auth, json={
        'name': 'happy.star.river', 'description': 'd',
        'voice_profile': {'engine': 'f5', 'preset': 'warm'}})
    assert resp.status_code == 201, resp.get_json()
    created = resp.get_json()['data']
    assert created['username'] == 'happy.star.river'
    assert 'voice_profile' not in created

    listed = client.get(f'/api/social/users/{user_id}/agents', headers=auth)
    assert listed.status_code == 200
    agents = listed.get_json()['data']
    assert [a['username'] for a in agents] == ['happy.star.river']
    assert all('voice_profile' not in a for a in agents)
