"""require_local_or_auth: loopback passes as before; a remote caller is a user.

The book routes are called by the desktop's own SPA from 127.0.0.1 with no
Authorization header, and must now ALSO be served by any HARTOS node to the
network, where an anonymous caller must not read or write anyone's library.

    python -m pytest tests/unit/test_require_local_or_auth.py -q --noconftest
"""
from unittest.mock import MagicMock, patch

from flask import Flask, g, jsonify

from integrations.social.auth import require_local_or_auth


def _client():
    app = Flask(__name__)

    @app.route('/probe')
    @require_local_or_auth
    def probe():
        return jsonify({'user_id': g.user_id})

    return app.test_client()


def test_a_local_caller_passes_without_a_token(monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    r = _client().get('/probe', environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert r.status_code == 200
    assert r.get_json() == {'user_id': None}


def test_a_remote_caller_without_a_token_is_refused(monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    monkeypatch.delenv('HEVOLVE_TRUST_KONG', raising=False)
    r = _client().get('/probe', environ_base={'REMOTE_ADDR': '10.0.0.7'})
    assert r.status_code == 401


def test_a_remote_caller_with_a_user_token_is_that_user(monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)
    user = MagicMock(id='u-42', is_banned=False)
    db = MagicMock()
    with patch('integrations.social.auth._get_user_from_token', return_value=(user, db)):
        r = _client().get('/probe', environ_base={'REMOTE_ADDR': '10.0.0.7'},
                          headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 200
    assert r.get_json() == {'user_id': 'u-42'}
    db.close.assert_called()


def test_a_remote_client_behind_the_trusted_proxy_is_still_remote(monkeypatch):
    """Same loopback test as require_local_or_token, TRUSTED_PROXY included:
    a proxy on 127.0.0.1 forwarding an outside client must not make that
    client local."""
    monkeypatch.setenv('TRUSTED_PROXY', '127.0.0.1')
    monkeypatch.delenv('HEVOLVE_TRUST_KONG', raising=False)
    r = _client().get('/probe', environ_base={'REMOTE_ADDR': '127.0.0.1'},
                      headers={'X-Forwarded-For': '203.0.113.9'})
    assert r.status_code == 401
