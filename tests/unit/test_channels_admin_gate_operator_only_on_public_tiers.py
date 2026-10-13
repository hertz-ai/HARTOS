"""On a public node, the channels admin API is the operator's, not every user's.

Measured on central 2026-10-13 (task #121): a throwaway user's JWT got 200
from GET /api/admin/status and /api/admin/channels, while the same JWT got
403 from /api/social/admin/*.  The gate in integrations/channels/admin/api.py
required only a valid token: its model was a single-user desktop ("any
registered (flat+) user can manage their own device").  Kong routes
/api/admin on the public API host with no host restriction, and the
blueprint carries POST/PUT/DELETE on channels, the queue, workflows, settings
and identity.  On the central and regional tiers the gate now requires the
operator (holds_central_role, the ONE check require_central applies); a flat
desktop keeps its single-user rule.

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
    u = MagicMock()
    u.id = attrs.pop('id', 7)
    u.role = attrs.pop('role', 'flat')
    u.is_admin = attrs.pop('is_admin', False)
    return u


def _get(client, user, path='/api/admin/version'):
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(user, MagicMock())):
        return client.get(path, headers={'Authorization': 'Bearer t'})


@pytest.mark.parametrize('tier', ['central', 'regional'])
def test_a_plain_user_is_refused_on_a_public_tier(monkeypatch, tier):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    r = _get(_client(), _user())
    assert r.status_code == 403, r.get_json()
    assert r.get_json()['success'] is False


def test_a_plain_user_cannot_write_on_a_public_tier(monkeypatch):
    """The gate runs before every route: a write is refused before the
    handler can touch the node's channel configuration."""
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(_user(), MagicMock())):
        r = _client().post('/api/admin/channels', json={'channel_type': 'discord'},
                           headers={'Authorization': 'Bearer t'})
    assert r.status_code == 403, r.get_json()


@pytest.mark.parametrize('operator', [
    dict(role='central'),            # the role UserService.set_user_role keeps
    dict(is_admin=True),             # the flag it keeps in step with it
])
def test_the_operator_is_admitted_on_a_public_tier(monkeypatch, operator):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r = _get(_client(), _user(**operator))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['data']['api_version']


def test_a_flat_desktop_keeps_its_single_user_rule(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    r = _get(_client(), _user())
    assert r.status_code == 200, r.get_json()


def test_no_token_is_still_401_everywhere(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r = _client().get('/api/admin/version')
    assert r.status_code == 401
