"""
Tests for User Consent Manager (ConsentService + UserConsent model).
Uses in-memory SQLite via HEVOLVE_DB_PATH=':memory:'.
"""
import os
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from integrations.social.models import Base, get_engine, get_db, db_session, UserConsent
from integrations.social.consent_service import (
    ConsentService, CONSENT_TYPES, _validate_consent_type,
    register_consent_routes,
)


@pytest.fixture(autouse=True)
def _fresh_db():
    """Create all tables before each test, drop after."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


# ──────────────────────────────────────────────────────────────────────
# request_consent
# ──────────────────────────────────────────────────────────────────────

def test_request_consent_creates_record():
    with db_session() as db:
        c = ConsentService.request_consent(db, 'u1', 'data_access')
    assert c is not None
    assert c.user_id == 'u1'
    assert c.consent_type == 'data_access'
    assert c.granted is False
    assert c.scope == '*'
    assert c.agent_id is None


def test_request_consent_returns_existing():
    with db_session() as db:
        c1 = ConsentService.request_consent(db, 'u1', 'data_access')
    with db_session() as db:
        c2 = ConsentService.request_consent(db, 'u1', 'data_access')
    assert c1.id == c2.id


def test_request_consent_invalid_type():
    with db_session() as db:
        with pytest.raises(ValueError, match='Invalid consent_type'):
            ConsentService.request_consent(db, 'u1', 'unknown_type')


# ──────────────────────────────────────────────────────────────────────
# grant_consent
# ──────────────────────────────────────────────────────────────────────

def test_grant_consent_new():
    with db_session() as db:
        c = ConsentService.grant_consent(db, 'u1', 'revenue_share')
    assert c.granted is True
    assert c.granted_at is not None
    assert c.revoked_at is None


def test_grant_consent_updates_existing():
    with db_session() as db:
        ConsentService.request_consent(db, 'u1', 'data_access')
    with db_session() as db:
        c = ConsentService.grant_consent(db, 'u1', 'data_access')
    assert c.granted is True
    assert c.granted_at is not None


@patch('integrations.social.consent_service._emit')
def test_grant_consent_emits_event(mock_emit):
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    mock_emit.assert_called_once_with('consent.granted', {
        'user_id': 'u1',
        'consent_type': 'data_access',
        'scope': '*',
        'agent_id': None,
    })


@patch('integrations.social.consent_service._audit')
def test_grant_consent_audits(mock_audit):
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'public_exposure', scope='profile')
    mock_audit.assert_called_once_with(
        'consent', actor_id='u1',
        action='consent.granted:public_exposure',
        detail={'scope': 'profile', 'agent_id': None})


def test_grant_consent_invalid_type():
    with db_session() as db:
        with pytest.raises(ValueError):
            ConsentService.grant_consent(db, 'u1', 'bad_type')


# ──────────────────────────────────────────────────────────────────────
# revoke_consent
# ──────────────────────────────────────────────────────────────────────

def test_revoke_consent_sets_revoked_at():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    with db_session() as db:
        c = ConsentService.revoke_consent(db, 'u1', 'data_access')
    assert c.granted is False
    assert c.revoked_at is not None


def test_revoke_consent_nonexistent_returns_none():
    with db_session() as db:
        result = ConsentService.revoke_consent(db, 'u1', 'data_access')
    assert result is None


@patch('integrations.social.consent_service._emit')
def test_revoke_consent_emits_event(mock_emit):
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'revenue_share')
    mock_emit.reset_mock()
    with db_session() as db:
        ConsentService.revoke_consent(db, 'u1', 'revenue_share')
    mock_emit.assert_called_once_with('consent.revoked', {
        'user_id': 'u1',
        'consent_type': 'revenue_share',
        'scope': '*',
        'agent_id': None,
    })


# ──────────────────────────────────────────────────────────────────────
# check_consent
# ──────────────────────────────────────────────────────────────────────

def test_check_consent_granted():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    with db_session() as db:
        assert ConsentService.check_consent(db, 'u1', 'data_access') is True


def test_check_consent_revoked():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    with db_session() as db:
        ConsentService.revoke_consent(db, 'u1', 'data_access')
    with db_session() as db:
        assert ConsentService.check_consent(db, 'u1', 'data_access') is False


def test_check_consent_not_found():
    with db_session() as db:
        assert ConsentService.check_consent(db, 'u1', 'data_access') is False


def test_check_consent_wildcard_scope():
    """Granting scope='*' covers specific scopes for same agent."""
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access',
                                     scope='*', agent_id='agent42')
    with db_session() as db:
        assert ConsentService.check_consent(
            db, 'u1', 'data_access', scope='photos', agent_id='agent42') is True


def test_check_consent_blanket_agent():
    """agent_id=None consent covers all agents."""
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access',
                                     scope='*', agent_id=None)
    with db_session() as db:
        assert ConsentService.check_consent(
            db, 'u1', 'data_access', scope='*', agent_id='agent99') is True


# ──────────────────────────────────────────────────────────────────────
# has_consent alias
# ──────────────────────────────────────────────────────────────────────

def test_has_consent_alias():
    assert ConsentService.has_consent is ConsentService.check_consent


# ──────────────────────────────────────────────────────────────────────
# list_consents
# ──────────────────────────────────────────────────────────────────────

def test_list_consents_all():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
        ConsentService.grant_consent(db, 'u1', 'revenue_share')
    with db_session() as db:
        records = ConsentService.list_consents(db, 'u1')
    assert len(records) == 2


def test_list_consents_filtered_by_type():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
        ConsentService.grant_consent(db, 'u1', 'revenue_share')
    with db_session() as db:
        records = ConsentService.list_consents(db, 'u1', consent_type='data_access')
    assert len(records) == 1
    assert records[0].consent_type == 'data_access'


def test_list_consents_filtered_by_agent():
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access', agent_id='a1')
        ConsentService.grant_consent(db, 'u1', 'data_access', agent_id='a2')
    with db_session() as db:
        records = ConsentService.list_consents(db, 'u1', agent_id='a1')
    assert len(records) == 1
    assert records[0].agent_id == 'a1'


# ──────────────────────────────────────────────────────────────────────
# UserConsent.to_dict
# ──────────────────────────────────────────────────────────────────────

def test_to_dict():
    with db_session() as db:
        c = ConsentService.grant_consent(db, 'u1', 'data_access', scope='photos')
        d = c.to_dict()
    assert d['user_id'] == 'u1'
    assert d['consent_type'] == 'data_access'
    assert d['scope'] == 'photos'
    assert d['granted'] is True
    assert d['granted_at'] is not None
    assert d['revoked_at'] is None


# ──────────────────────────────────────────────────────────────────────
# Flask route tests
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def flask_client():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_consent_routes(app)
    return app.test_client()


def test_route_get_list(flask_client):
    # Seed data
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    resp = flask_client.get('/api/consent/u1')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]['consent_type'] == 'data_access'


def test_route_post_grant(flask_client):
    resp = flask_client.post('/api/consent/u1',
                             json={'consent_type': 'revenue_share'})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['granted'] is True
    assert data['consent_type'] == 'revenue_share'


def test_route_post_grant_missing_type(flask_client):
    resp = flask_client.post('/api/consent/u1', json={})
    assert resp.status_code == 400


def test_route_post_grant_invalid_type(flask_client):
    resp = flask_client.post('/api/consent/u1',
                             json={'consent_type': 'bad'})
    assert resp.status_code == 400


def test_route_post_revoke(flask_client):
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    resp = flask_client.post('/api/consent/u1/revoke',
                             json={'consent_type': 'data_access'})
    assert resp.status_code == 200
    assert resp.get_json()['granted'] is False


def test_route_post_revoke_not_found(flask_client):
    resp = flask_client.post('/api/consent/u1/revoke',
                             json={'consent_type': 'data_access'})
    assert resp.status_code == 404


def test_route_get_check(flask_client):
    with db_session() as db:
        ConsentService.grant_consent(db, 'u1', 'data_access')
    resp = flask_client.get('/api/consent/u1/check?type=data_access')
    assert resp.status_code == 200
    assert resp.get_json()['granted'] is True


def test_route_get_check_missing_type(flask_client):
    resp = flask_client.get('/api/consent/u1/check')
    assert resp.status_code == 400


def test_route_get_check_not_granted(flask_client):
    resp = flask_client.get('/api/consent/u1/check?type=data_access')
    assert resp.status_code == 200
    assert resp.get_json()['granted'] is False
