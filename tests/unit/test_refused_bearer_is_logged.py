"""A request refused because no way in accepted its credential is said once
in the log, where the request is refused, by the credential's shape.

The owner's rules, as hartos-70 holds them: H-0711 "all errors shd be
propogated and thrown and seen in logs", N-0503 "there are redudnacies
across logs but nothing shd be gulped".  Since 304b0cf4a a credential with
fewer than three segments is no longer tried as a JWT, so JWTManager no
longer warns about it, and a request carrying an unknown one was refused
with no line anywhere: measured on the desktop 2026-10-21 15:54,
/api/social/auth/me with an unknown one-segment bearer answered 401 and the
log said nothing.

The line is written where the request is refused, not inside
_get_user_from_token: for some of its callers None is not a refusal (a
loopback caller falls back to the desktop's owner, an optional login goes
on anonymous), and a line there would warn about requests that succeed.
_get_user_from_token leaves the reason on flask.g; log_refused_credential
writes it with the shape, never the credential.
"""
import logging
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from flask import Flask, g, jsonify, request

import integrations.social.auth as auth
from security.secret_redactor import redact_secrets

UNKNOWN_OPAQUE = 'opaque-credential-no-account-has-it'


def _db_returning(user):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    return db


def _user(uid='10202', banned=False):
    u = MagicMock()
    u.id = uid
    u.is_banned = banned
    return u


class _Records(logging.Handler):
    """Records as auth's own logger hands them on: read there, before the
    root handlers, whose security.audit_log filter rewrites a record's
    message in place once hart_intelligence_entry has been imported."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.seen = []

    def emit(self, record):
        self.seen.append(record)

    def refusals(self):
        # Either place a refusal line could come from, so a guard below also
        # fails if the line moves back into the lookup.
        return [r for r in self.seen
                if r.funcName in ('log_refused_credential', '_get_user_from_token')
                and r.levelno >= logging.WARNING]


@pytest.fixture
def social_log():
    log = logging.getLogger('hevolve_social')
    records = _Records()
    level = log.level
    log.addHandler(records)
    log.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        log.removeHandler(records)
        log.setLevel(level)


@pytest.fixture(autouse=True)
def _hardened_manager(monkeypatch):
    # The manager auth uses whenever it can build one, with the test's key.
    from security.jwt_manager import JWTManager
    monkeypatch.setattr(auth, '_jwt_manager', JWTManager(secret_key='s' * 64))
    monkeypatch.delenv('HEVOLVE_TRUST_KONG', raising=False)
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/who')
    @auth.require_auth
    def who():
        return jsonify({'user_id': g.user_id})

    @app.route('/maybe')
    @auth.optional_auth
    def maybe():
        return jsonify({'user_id': g.user_id})

    return app


def _get(app, path, token, user=None):
    with patch('integrations.social.models.get_db',
               return_value=_db_returning(user)):
        return app.test_client().get(
            path, headers={'Authorization': f'Bearer {token}'})


class TestARefusedRequestIsSaidOnce:

    def test_an_unknown_opaque_bearer_is_refused_with_one_line(self, app, social_log):
        r = _get(app, '/who', UNKNOWN_OPAQUE)
        assert r.status_code == 401
        (line,) = social_log.refusals()
        assert line.levelno == logging.WARNING
        assert 'unknown_token' in line.getMessage()

    def test_the_line_gives_the_shape_never_the_credential(self, app, social_log):
        _get(app, '/who', UNKNOWN_OPAQUE)
        (line,) = [r.getMessage() for r in social_log.refusals()]
        assert UNKNOWN_OPAQUE not in line
        assert str(len(UNKNOWN_OPAQUE)) in line
        assert '/who' in line

    @pytest.mark.parametrize('reason, token, user', [
        ('unknown_token', UNKNOWN_OPAQUE, None),
        ('api_token_user_banned', UNKNOWN_OPAQUE, _user(banned=True)),
        ('jwt_user_missing', 'jwt', None),
        ('jwt_user_banned', 'jwt', _user('u-local', banned=True)),
    ])
    def test_each_refusal_says_why_and_reaches_the_log_as_written(
            self, app, social_log, reason, token, user):
        if token == 'jwt':
            token = auth.generate_jwt('u-local', 'someone', 'flat')
        r = _get(app, '/who', token, user)
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert reason in line
        # The filter on the app's handlers turned "Invalid token: Not enough
        # segments" into "Invalid [REDACTED:secret_assignment] enough
        # segments".  Nothing in this line may look like a secret to it.
        assert redact_secrets(line) == (line, 0)

    def test_a_jwt_that_does_not_verify_is_refused_with_one_line(self, app, social_log):
        foreign = pyjwt.encode({'user_id': 'u-x', 'type': 'access'}, 'f' * 64,
                               algorithm='HS256')
        r = _get(app, '/who', foreign)
        assert r.status_code == 401
        assert len(social_log.refusals()) == 1


class TestAnAcceptedAbsentOrUnrefusedCredentialSaysNothing:

    def test_a_stored_api_token_is_no_refusal(self, app, social_log):
        r = _get(app, '/who', 'a' * 32, _user('10202'))
        assert r.status_code == 200
        assert social_log.refusals() == []

    def test_a_local_jwt_is_no_refusal(self, app, social_log):
        token = auth.generate_jwt('u-local', 'someone', 'flat')
        r = _get(app, '/who', token, _user('u-local'))
        assert r.status_code == 200
        assert social_log.refusals() == []

    def test_no_bearer_behind_kong_is_no_refusal(self, app, social_log, monkeypatch):
        # Kong passed the request on and vouches for nobody: an anonymous
        # request, not a credential this node refused.
        monkeypatch.setenv('HEVOLVE_TRUST_KONG', 'true')
        with patch('integrations.social.models.get_db',
                   return_value=_db_returning(None)):
            r = app.test_client().get(
                '/who', headers={'X-Anonymous-Consumer': 'true'})
        assert r.status_code == 401
        assert social_log.refusals() == []

    def test_an_optional_login_goes_on_anonymous_without_a_line(self, app, social_log):
        r = _get(app, '/maybe', UNKNOWN_OPAQUE)
        assert r.status_code == 200
        assert r.get_json()['user_id'] is None
        assert social_log.refusals() == []

    def test_this_machines_caller_falls_back_to_the_owner_without_a_line(
            self, social_log, monkeypatch):
        from integrations.social.dashboard_service import steering_caller
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
        monkeypatch.delenv('NUNBA_CI', raising=False)
        with Flask(__name__).test_request_context(
                '/api/social/dashboard/agents/x',
                headers={'Authorization': f'Bearer {UNKNOWN_OPAQUE}'},
                environ_base={'REMOTE_ADDR': '127.0.0.1'}), \
                patch('integrations.social.models.get_db',
                      return_value=_db_returning(None)):
            caller = steering_caller()
        assert (caller.user_id, caller.is_local) == ('owner-1', True)
        assert social_log.refusals() == []

    def test_a_node_key_names_no_approver_and_the_lookup_says_nothing(self, social_log):
        from integrations.commerce.approvals import approver_from_request
        with Flask(__name__).test_request_context(
                '/api/agent/approval',
                headers={'Authorization': 'Bearer node-api-key-0123'}), \
                patch('integrations.social.models.get_db',
                      return_value=_db_returning(None)):
            assert approver_from_request(request) is None
        assert social_log.refusals() == []


# The routes that resolve a bearer through _get_user_from_token themselves and
# answer its None with a 401 of their own instead of going through
# require_auth.  Each is a place the request is refused, so each says so.
_SELF_REFUSING_ROUTES = [
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'POST', '/api/v1/intelligence/keys'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'GET', '/api/v1/intelligence/keys'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'DELETE', '/api/v1/intelligence/keys/k1'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'POST', '/api/v1/intelligence/keys/k1/upgrade'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'POST', '/api/v1/intelligence/keys/k1/upgrade/phonepe'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'POST', '/api/v1/intelligence/keys/k1/upgrade/checkout'),
    ('integrations.agent_engine.commercial_api', 'commercial_api_bp',
     'POST', '/api/v1/intelligence/upgrade/checkout/complete'),
    ('integrations.agent_engine.build_distribution', 'build_distribution_bp',
     'POST', '/api/v1/builds/purchase'),
    ('integrations.agent_engine.build_distribution', 'build_distribution_bp',
     'GET', '/api/v1/builds/download/l1'),
    ('integrations.agent_engine.build_distribution', 'build_distribution_bp',
     'GET', '/api/v1/builds/licenses'),
    ('integrations.channels.oauth_api', 'oauth_bp',
     'POST', '/api/oauth/discord/start'),
    ('integrations.channels.admin.api', 'admin_bp',
     'GET', '/api/admin/channels'),
]
_ROUTE_IDS = [f'{method} {path}' for _, _, method, path in _SELF_REFUSING_ROUTES]


def _mounted(module, blueprint):
    import importlib
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(getattr(importlib.import_module(module), blueprint))
    return app


def _send(app, method, path, headers, body=None, remote='127.0.0.1'):
    with patch('integrations.social.models.get_db',
               return_value=_db_returning(None)):
        return app.test_client().open(path, method=method, headers=headers,
                                      json=body or {},
                                      environ_base={'REMOTE_ADDR': remote})


class TestEveryRouteThatRefusesABearerItselfSaysSo:

    @pytest.mark.parametrize('module, blueprint, method, path',
                             _SELF_REFUSING_ROUTES, ids=_ROUTE_IDS)
    def test_an_unknown_bearer_is_refused_with_one_line(
            self, social_log, module, blueprint, method, path):
        r = _send(_mounted(module, blueprint), method, path,
                  {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'})
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert f'{method} {path}' in line
        assert 'unknown_token' in line
        assert UNKNOWN_OPAQUE not in line

    @pytest.mark.parametrize('module, blueprint, method, path',
                             _SELF_REFUSING_ROUTES, ids=_ROUTE_IDS)
    def test_a_request_with_no_bearer_is_refused_without_a_line(
            self, social_log, module, blueprint, method, path):
        r = _send(_mounted(module, blueprint), method, path, {})
        assert r.status_code == 401
        assert social_log.refusals() == []


# The commerce routes and the approval answer read the bearer through
# approvals.approver_from_request, which answers None for any credential
# that names no person; each of them turns that None into a 401.
_COMMERCE_READS = ['/api/commerce/mandates/m1', '/api/commerce/stream']


class TestTheCommerceRefusalsSaySo:

    @pytest.mark.parametrize('path', _COMMERCE_READS)
    def test_an_unknown_bearer_is_refused_with_one_line(self, social_log, path):
        app = _mounted('integrations.commerce.commerce_api', 'commerce_bp')
        r = _send(app, 'GET', path, {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'})
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert f'GET {path}' in line and 'unknown_token' in line

    @pytest.mark.parametrize('path', _COMMERCE_READS)
    def test_no_bearer_is_refused_without_a_line(self, social_log, path):
        app = _mounted('integrations.commerce.commerce_api', 'commerce_bp')
        r = _send(app, 'GET', path, {})
        assert r.status_code == 401
        assert social_log.refusals() == []

    @pytest.fixture
    def shell_app(self, monkeypatch, tmp_path):
        # LiquidUI's own /api/agent/approval; hart_intelligence_entry's
        # agent_approval answers a commerce card through the same
        # answer_commerce_approval.
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
        with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                   return_value=False):
            yield LiquidUIService(a2ui_enabled=False)._create_flask_app()

    def test_a_payment_answer_with_an_unknown_bearer_is_refused_with_one_line(
            self, social_log, shell_app):
        r = _send(shell_app, 'POST', '/api/agent/approval',
                  {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'},
                  body={'agent_id': 'a1', 'action': 'ap2_pay:p1',
                        'decision': 'approve'})
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert 'POST /api/agent/approval' in line and 'unknown_token' in line

    def test_a_payment_answer_with_no_bearer_is_refused_without_a_line(
            self, social_log, shell_app):
        r = _send(shell_app, 'POST', '/api/agent/approval', {},
                  body={'agent_id': 'a1', 'action': 'ap2_pay:p1',
                        'decision': 'approve'})
        assert r.status_code == 401
        assert social_log.refusals() == []


def _gated(monkeypatch, bundled):
    """An app behind the real API gate (security.middleware), with a
    network-protected route and an admin route."""
    from security.middleware import _apply_api_auth
    for var in ('HEVOLVE_API_KEY', 'HEVOLVE_OWNER_USER_ID', 'NUNBA_CI',
                'HEVOLVE_NODE_TIER'):
        monkeypatch.delenv(var, raising=False)
    if bundled:
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
    else:
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setattr('security.secrets_manager.get_secret',
                        lambda name, *a, **k: '', raising=False)
    app = Flask(__name__)
    app.config['TESTING'] = True

    @app.route('/chat', methods=['POST'])
    def chat():
        return jsonify({'ok': True})

    @app.route('/api/admin/channels')
    def admin_channels():
        return jsonify({'ok': True})

    _apply_api_auth(app)
    return app


class TestTheApiGateSaysItsFinalRefusal:

    def test_another_machine_with_an_unknown_bearer_is_refused_with_one_line(
            self, social_log, monkeypatch):
        r = _send(_gated(monkeypatch, bundled=True), 'POST', '/chat',
                  {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'},
                  remote='192.168.1.50')
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert 'POST /chat from 192.168.1.50' in line and 'jwt_invalid' in line

    def test_an_admin_path_with_an_unknown_bearer_is_refused_with_one_line(
            self, social_log, monkeypatch):
        r = _send(_gated(monkeypatch, bundled=False), 'GET',
                  '/api/admin/channels',
                  {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'})
        assert r.status_code == 401
        (line,) = [rec.getMessage() for rec in social_log.refusals()]
        assert 'GET /api/admin/channels' in line and 'jwt_invalid' in line

    def test_another_machine_with_no_bearer_is_refused_without_a_line(
            self, social_log, monkeypatch):
        r = _send(_gated(monkeypatch, bundled=True), 'POST', '/chat', {},
                  remote='192.168.1.50')
        assert r.status_code == 401
        assert social_log.refusals() == []

    def test_this_machine_passes_without_a_line(self, social_log, monkeypatch):
        r = _send(_gated(monkeypatch, bundled=True), 'POST', '/chat',
                  {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'})
        assert r.status_code == 200
        assert social_log.refusals() == []

    def test_a_phone_the_owner_allowed_passes_without_a_line(
            self, social_log, monkeypatch):
        # The gate's first check refuses a phone's device token (it is no
        # local JWT); the owner's grant admits it right after.  The first
        # refusal is not the request's, so it says nothing.
        from contextlib import contextmanager

        @contextmanager
        def _session(commit=False):
            yield MagicMock()

        app = _gated(monkeypatch, bundled=True)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
        with patch('integrations.social.models.db_session', _session), \
                patch('integrations.social.auth.verify_device_jwt',
                      return_value={'status': 'ok', 'public_key': 'ab' * 32,
                                    'payload': {'user_id': 'u-phone'}}):
            r = _send(app, 'POST', '/chat',
                      {'Authorization': f'Bearer {UNKNOWN_OPAQUE}'},
                      body={'user_id': 'u-phone'}, remote='192.168.1.50')
        assert r.status_code == 200
        assert social_log.refusals() == []
