"""A paid API tier is bought, never self-issued.

Read in full on 2026-10-12 while validating central live: POST
/api/v1/intelligence/keys passed the body's ``tier`` straight to
CommercialAPIService.create_api_key, so any signed-in person could mint a
``pro`` or ``enterprise`` key with its quotas for nothing.  With the lanes
(owner policy 2026-10-12) a paid-tier key also puts the turn on the revenue
lane, where the Claude expert may serve it: the hole would have spent the
Claude budget on unpaid callers.  The upgrade routes (AP2 ledger, Stripe,
PhonePe) are the one way a key gains a paid tier.

These drive the real blueprint route; only the token-to-user lookup and the
database are stubbed.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


@pytest.fixture
def client():
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with app.test_client() as c:
        yield c


def _signed_in(is_admin=False):
    user = types.SimpleNamespace(id='u-1', is_admin=is_admin)
    db = MagicMock()
    return patch('integrations.social.auth._get_user_from_token', return_value=(user, db)), db


@pytest.mark.parametrize('tier', ['starter', 'pro', 'enterprise'])
def test_a_person_cannot_self_issue_a_paid_tier(client, tier):
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in()
    with lookup, patch.object(CommercialAPIService, 'create_api_key') as create:
        r = client.post('/api/v1/intelligence/keys', json={'name': 'x', 'tier': tier},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['success'] is False and 'upgrade' in body['error'].lower()
    create.assert_not_called()                  # nothing minted, nothing to revoke
    db.commit.assert_not_called()


def test_a_person_still_gets_a_free_key(client):
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in()
    with lookup, patch.object(CommercialAPIService, 'create_api_key',
                              return_value={'id': 'k1', 'tier': 'free', 'raw_key': 'r'}) as create:
        r = client.post('/api/v1/intelligence/keys', json={'name': 'x'},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 201 and r.get_json()['api_key']['tier'] == 'free'
    assert create.call_args.kwargs['tier'] == 'free'
    r2_lookup, _ = _signed_in()
    with r2_lookup, patch.object(CommercialAPIService, 'create_api_key',
                                 return_value={'id': 'k2', 'tier': 'free', 'raw_key': 'r'}):
        r2 = client.post('/api/v1/intelligence/keys', json={'name': 'x', 'tier': 'free'},
                         headers={'Authorization': 'Bearer tok'})
    assert r2.status_code == 201


def test_an_admin_may_issue_a_paid_key_directly(client):
    """Operators grant tiers by hand (a sponsored partner, a refund); the
    person's own path stays the upgrade route."""
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in(is_admin=True)
    with lookup, patch.object(CommercialAPIService, 'create_api_key',
                              return_value={'id': 'k3', 'tier': 'pro', 'raw_key': 'r'}) as create:
        r = client.post('/api/v1/intelligence/keys', json={'name': 'partner', 'tier': 'pro'},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 201
    assert create.call_args.kwargs['tier'] == 'pro'


def test_an_unknown_tier_is_still_rejected_by_the_service_rule(client):
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in(is_admin=True)
    with lookup, patch.object(CommercialAPIService, 'create_api_key',
                              return_value={'error': 'Invalid tier: gold. Valid: [...]'}):
        r = client.post('/api/v1/intelligence/keys', json={'tier': 'gold'},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 400 and 'Invalid tier' in r.get_json()['error']
