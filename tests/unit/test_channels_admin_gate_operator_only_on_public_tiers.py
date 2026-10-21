"""On central, the channels admin API is the operator's, not every user's.

Measured on central 2026-10-13 (task #121): a throwaway user's JWT got 200
from GET /api/admin/status and /api/admin/channels, while the same JWT got
403 from /api/social/admin/*.  The gate in integrations/channels/admin/api.py
required only a valid token: its model was a single-user desktop ("any
registered (flat+) user can manage their own device").  Kong routes
/api/admin on the public API host with no host restriction, and the
blueprint carries POST/PUT/DELETE on channels, the queue, workflows, settings
and identity.  On central the gate now requires the operator
(holds_central_role: role 'central', or the is_admin flag kept in step with
it); a flat desktop keeps its single-user rule.

Regional keeps the single-user rule too, on purpose: a regional node's own
operator holds neither role 'central' nor is_admin (no code path grants
either locally; the most such an account carries is role 'regional' with
is_moderator, what UserService.set_user_role writes), which holds_central_role
does not recognise, and the first version of this gate locked every regional
operator out (review of 91a211c9e).  Who operates a regional node is the
owner's ruling, task #124.

Drives the real blueprint and the real gate; only the token lookup is stubbed.
"""
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


def _client():
    from integrations.channels.admin.api import admin_bp
    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    return app.test_client()


def _user(**attrs):
    """A plain registered user unless told otherwise: every flag explicit, so
    a check that reads a flag never meets a truthy MagicMock by accident."""
    u = MagicMock()
    u.id = attrs.pop('id', 7)
    u.role = attrs.pop('role', 'flat')
    u.is_admin = attrs.pop('is_admin', False)
    u.is_moderator = attrs.pop('is_moderator', False)
    return u


def _regional_operator():
    # What UserService.set_user_role('regional') writes.
    return _user(role='regional', is_admin=False, is_moderator=True)


def _get(client, user, path='/api/admin/version', db=None):
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(user, db if db is not None else MagicMock())):
        return client.get(path, headers={'Authorization': 'Bearer t'})


@pytest.mark.parametrize('tier', ['central', 'Central', 'CENTRAL'])
def test_a_plain_user_is_refused_on_central(monkeypatch, tier):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    r = _get(_client(), _user())
    assert r.status_code == 403, r.get_json()
    assert r.get_json()['success'] is False


def test_a_plain_user_cannot_write_on_central(monkeypatch):
    """The gate runs before every route: a write is refused before the
    handler can touch the node's channel configuration."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(_user(), MagicMock())):
        r = _client().post('/api/admin/channels', json={'channel_type': 'discord'},
                           headers={'Authorization': 'Bearer t'})
    assert r.status_code == 403, r.get_json()


def test_the_refusal_closes_the_db_session(monkeypatch):
    """The teardown closes g.db only when the gate set it; the 403 path must
    close the session it opened itself."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    db = MagicMock()
    r = _get(_client(), _user(), db=db)
    assert r.status_code == 403
    db.close.assert_called_once()


def test_a_regional_operator_is_not_centrals_operator(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r = _get(_client(), _regional_operator())
    assert r.status_code == 403, r.get_json()


@pytest.mark.parametrize('operator', [
    dict(role='central'),            # the role UserService.set_user_role keeps
    dict(is_admin=True),             # the flag it keeps in step with it
])
def test_the_operator_is_admitted_on_central(monkeypatch, operator):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r = _get(_client(), _user(**operator))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['data']['api_version']


@pytest.mark.parametrize('user', [_regional_operator(), _user()],
                         ids=['regional operator', 'plain user'])
def test_a_regional_node_keeps_its_rule_until_the_owner_rules(monkeypatch, user):
    """Pins the deliberate scope: regional is not on the operator-only list,
    so its operator (role 'regional') is not locked out.  Task #124 decides
    the regional rule; this test changes with it."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
    r = _get(_client(), user)
    assert r.status_code == 200, r.get_json()


def test_a_flat_desktop_keeps_its_single_user_rule(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    r = _get(_client(), _user())
    assert r.status_code == 200, r.get_json()


def test_no_token_is_still_401_everywhere(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r = _client().get('/api/admin/version')
    assert r.status_code == 401
