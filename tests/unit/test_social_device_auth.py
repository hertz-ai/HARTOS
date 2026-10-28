"""Social requests use the same owner-approved phone proof as the desktop gate."""
from unittest.mock import patch
from flask import Flask, g, jsonify
from .test_device_access_gate import (
    Phone, OWNER, LAN, _desktop_env, _allow, _bearer,
)
from integrations.social.auth import require_auth
from integrations.social.models import User, db_session


def client():
    app = Flask(__name__)
    app.testing = True
    # A Social route, the consent answers, and a route outside Social.
    for path in ('/api/social/test', '/api/social/consent/test', '/api/agent/test'):
        @app.route(path, methods=['GET', 'POST'], endpoint=path)
        @require_auth
        def who():
            return jsonify({'id': g.user_id})
    return app.test_client()


def owner_phone():
    phone = Phone(user_id=OWNER)
    with db_session() as db:
        db.add(User(id=OWNER, username='owner', email='owner@example.invalid', user_type='human'))
    return phone


def test_owner_approved_phone_can_use_social():
    phone = owner_phone()
    _allow(phone)
    with patch('integrations.social.feature_flags.get_flags_for_tenant', return_value={}):
        response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 200
    assert response.json['id'] == OWNER


def test_unapproved_phone_gets_consent_pending():
    phone = owner_phone()
    with patch('integrations.social.consent_service._emit'):
        response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 403
    assert response.json['error'] == 'consent_pending'


def test_allowed_phone_cannot_claim_another_social_account():
    phone = owner_phone()
    _allow(phone)
    phone.user_id = 'someone-else'
    response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 401


def test_forged_signature_and_expired_proof_stay_unauthorized():
    phone = owner_phone()
    _allow(phone)
    for token in (phone.token(signer=Phone()), phone.token(exp_in=-60)):
        response = client().get('/api/social/test', headers=_bearer(token), environ_base=LAN)
        assert response.status_code == 401


def test_device_admission_is_desktop_only(monkeypatch):
    phone = owner_phone()
    _allow(phone)
    monkeypatch.delenv('NUNBA_BUNDLED')
    response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 401


def test_phone_cannot_override_user_in_body():
    phone = owner_phone()
    _allow(phone)
    response = client().post('/api/social/test', headers=_bearer(phone.token()),
                             json={'user_id': 'someone-else'}, environ_base=LAN)
    assert response.status_code == 403
    assert response.json['error'] == "user_id must be the token's user"


def test_social_post_need_not_name_the_user():
    # The phone's Social bodies (a location ping) carry no user_id.
    phone = owner_phone()
    _allow(phone)
    with patch('integrations.social.feature_flags.get_flags_for_tenant', return_value={}):
        response = client().post('/api/social/test', headers=_bearer(phone.token()),
                                 json={'lat': 1.0, 'lon': 2.0}, environ_base=LAN)
    assert response.status_code == 200
    assert response.json['id'] == OWNER


def test_approved_phone_is_not_the_owner_outside_social():
    phone = owner_phone()
    _allow(phone)
    response = client().get('/api/agent/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 401


def test_approved_phone_cannot_answer_consent_asks():
    # Otherwise one allowed phone could allow another phone, or a feed.
    phone = owner_phone()
    _allow(phone)
    response = client().post('/api/social/consent/test', headers=_bearer(phone.token()),
                             json={}, environ_base=LAN)
    assert response.status_code == 401


def test_approved_phone_cannot_approve_a_payment():
    # Review of #147: an approved phone posting /api/agent/approval
    # {action: 'ap2_pay:<id>'} resolved to the owner and the payment went
    # through.  The approver lookup must not know the phone.
    from integrations.commerce.approvals import approver_from_request
    from integrations.social.auth import user_id_for_token
    phone = owner_phone()
    _allow(phone)
    app = Flask(__name__)
    with app.test_request_context('/api/agent/approval', method='POST',
                                  headers=_bearer(phone.token()), environ_base=LAN,
                                  json={'action': 'ap2_pay:x', 'decision': 'approve',
                                        'user_id': OWNER}):
        from flask import request
        assert user_id_for_token(phone.token()) is None
        assert approver_from_request(request) is None


def test_a_failing_device_check_is_401_not_500():
    phone = owner_phone()
    _allow(phone)
    with patch('integrations.social.auth.verify_device_jwt', side_effect=RuntimeError('db down')):
        response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 401


def test_revoked_phone_is_denied_without_a_new_ask():
    from integrations.social.consent_service import ConsentService, device_scope
    phone = owner_phone()
    _allow(phone)
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'device_access', scope=device_scope(phone.public_hex))
    with patch('integrations.social.consent_service._emit') as emit:
        response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 403
    assert response.json['error'] == 'consent_denied'
    emit.assert_not_called()


def test_banned_owner_is_not_admitted_from_an_approved_phone():
    phone = owner_phone()
    _allow(phone)
    with db_session() as db:
        db.query(User).filter_by(id=OWNER).one().is_banned = True
    response = client().get('/api/social/test', headers=_bearer(phone.token()), environ_base=LAN)
    assert response.status_code == 401


def test_a_token_claiming_a_long_life_is_refused():
    # The apps mint 15-minute tokens; one that says it lives an hour is
    # refused, so a sniffed token stays useful for minutes only.
    phone = owner_phone()
    _allow(phone)
    response = client().get('/api/social/test', headers=_bearer(phone.token(exp_in=3600)),
                            environ_base=LAN)
    assert response.status_code == 401
