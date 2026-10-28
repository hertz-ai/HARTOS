"""One "may this person act here" rule for every door (integrations.social
.auth.user_refusal): no account -> 'no_user', banned -> 'banned', else None.

The Bearer doors (require_auth through _get_user_from_token, and
user_id_for_token), the steering caller (dashboard_service) and the phone's
device-link door (phone_call, tested beside it in test_phone_call_open.py)
all ask it, so a refusal added once reaches them all.  Before, each door
re-read is_banned for itself and the phone door ran its own SQL
(parallel-path audit 2026-10-27, item 4).

Each test here drives the real door with the real lookup around it; only
the database and the JWT decode are stood in for.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest
from flask import Flask, g, jsonify

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from integrations.social import auth  # noqa: E402


def _user(user_id='u-1', banned=False):
    user = MagicMock()
    user.id = user_id
    user.is_banned = banned
    user.role = 'flat'
    user.is_admin = False
    return user


@pytest.fixture
def jwt_door(monkeypatch):
    """A Bearer the JWT decode accepts, looked up in a database that holds
    ``user``: the path require_auth and user_id_for_token walk."""
    def _install(user):
        db = MagicMock()
        db.is_active = True
        db.query.return_value.filter.return_value.first.return_value = user
        monkeypatch.setattr(auth, 'decode_jwt',
                            lambda token: {'user_id': 'u-1', 'scope': 'local'})
        import integrations.social.models as models
        monkeypatch.setattr(models, 'get_db', lambda: db)
        return db
    return _install


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/who')
    @auth.require_auth
    def who():
        return jsonify({'user_id': g.user_id})
    return app


@pytest.fixture(autouse=True)
def _flat(monkeypatch):
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)
    monkeypatch.delenv('HEVOLVE_TRUST_KONG', raising=False)


def test_the_rule_names_the_refusal():
    assert auth.user_refusal(None) == 'no_user'
    assert auth.user_refusal(_user(banned=True)) == 'banned'
    assert auth.user_refusal(_user()) is None


def test_require_auth_admits_and_refuses_by_the_one_rule(app, jwt_door, monkeypatch):
    jwt_door(_user())
    client = app.test_client()
    assert client.get('/who', headers={'Authorization': 'Bearer t'}).status_code == 200
    # a refusal added to the rule, not to the door, refuses at the door
    monkeypatch.setattr(auth, 'user_refusal', lambda user: 'on_leave', raising=False)
    r = client.get('/who', headers={'Authorization': 'Bearer t'})
    assert r.status_code == 401
    assert r.get_json()['needs_sign_in'] is True


def test_require_auth_refuses_a_banned_user_through_the_rule(app, jwt_door):
    jwt_door(_user(banned=True))
    r = app.test_client().get('/who', headers={'Authorization': 'Bearer t'})
    assert r.status_code == 401


def test_user_id_for_token_answers_by_the_one_rule(jwt_door, monkeypatch):
    jwt_door(_user())
    assert auth.user_id_for_token('t') == 'u-1'
    monkeypatch.setattr(auth, 'user_refusal', lambda user: 'on_leave', raising=False)
    assert auth.user_id_for_token('t') is None


def test_user_id_for_token_refuses_a_banned_user_through_the_rule(jwt_door):
    jwt_door(_user(banned=True))
    assert auth.user_id_for_token('t') is None


def test_the_steering_caller_falls_back_to_the_owner_by_the_one_rule(app, jwt_door, monkeypatch):
    """dashboard_service's caller resolution: a token whose user the rule
    refuses is no token, and the loopback owner answers instead."""
    from integrations.social import dashboard_service as ds
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-9')
    monkeypatch.setattr('core.auth_local._is_local_request', lambda: True)
    jwt_door(_user())
    with app.test_request_context('/x', headers={'Authorization': 'Bearer t'}):
        assert ds.steering_caller().user_id == 'u-1'
    monkeypatch.setattr(auth, 'user_refusal', lambda user: 'on_leave', raising=False)
    with app.test_request_context('/x', headers={'Authorization': 'Bearer t'}):
        assert ds.steering_caller().user_id == 'owner-9'
