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
    @app.route('/api/social/test', methods=['GET', 'POST'])
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
