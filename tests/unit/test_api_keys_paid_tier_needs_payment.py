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


@pytest.mark.parametrize('tier', ['starter', 'pro', 'enterprise', 'PRO', ' enterprise '])
def test_a_person_cannot_self_issue_a_paid_tier(client, tier):
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in()
    with lookup, patch.object(CommercialAPIService, 'create_api_key',
                              return_value={'id': 'k', 'tier': tier, 'raw_key': 'r'}) as create:
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


@pytest.mark.parametrize('is_admin', [False, True])
@pytest.mark.parametrize('tier', ['gold', 5, True, ['pro']])
def test_an_unknown_or_non_string_tier_is_the_tables_400_for_everyone(client, tier, is_admin):
    """Review of 0cf610371: a plain user asking for 'gold' was told it is
    'bought', and an int tier crashed to 500 on .strip()."""
    from integrations.agent_engine.commercial_api import CommercialAPIService
    lookup, db = _signed_in(is_admin=is_admin)
    with lookup, patch.object(CommercialAPIService, 'create_api_key') as create:
        r = client.post('/api/v1/intelligence/keys', json={'tier': tier},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 400 and 'Invalid tier' in r.get_json()['error']
    create.assert_not_called()


def test_an_operator_by_role_may_issue_a_paid_key_and_the_tier_is_normalised(client):
    from integrations.agent_engine.commercial_api import CommercialAPIService
    user = types.SimpleNamespace(id='u-2', is_admin=False, role='central')
    db = MagicMock()
    with patch('integrations.social.auth._get_user_from_token', return_value=(user, db)), \
            patch.object(CommercialAPIService, 'create_api_key',
                         return_value={'id': 'k4', 'tier': 'pro', 'raw_key': 'r'}) as create:
        r = client.post('/api/v1/intelligence/keys', json={'tier': ' PRO '},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 201
    assert create.call_args.kwargs['tier'] == 'pro'


def test_self_issuable_tiers_are_exactly_the_free_ones():
    from integrations.agent_engine.commercial_api import SELF_ISSUABLE_TIERS, TIER_CONFIG
    assert SELF_ISSUABLE_TIERS == frozenset(
        t for t, c in TIER_CONFIG.items() if c['monthly_price_usd'] == 0)
    assert 'free' in SELF_ISSUABLE_TIERS and 'pro' not in SELF_ISSUABLE_TIERS


# ─── the upgrade route: the mock gateway settles nothing for a customer ───────
# Review of 0cf610371 (measured on the real route): PaymentLedger always
# registers MockPaymentGateway, upgrade_key honoured a caller-named
# ``gateway: mock`` and fell back to Mock when Stripe was absent, and the mock
# authorizes any payment_method string.  A plain user got enterprise for
# nothing, with the $499 recorded in the ledger as paid.  The caller never
# chooses the gateway now; a Mock result is refused unless the caller is an
# operator or the node is in dev mode.

from decimal import Decimal  # noqa: E402

from integrations.ap2.ap2_protocol import (  # noqa: E402
    PaymentGateway, PaymentGatewayConnector, PaymentLedger,
)


class _DecliningStripe(PaymentGatewayConnector):
    """A registered Stripe connector whose card is declined."""

    def __init__(self):
        super().__init__(PaymentGateway.STRIPE)

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        return {'success': False, 'error': 'card_declined'}


def _upgrade(client, tmp_path, monkeypatch, *, is_admin=False, stripe=False, dev_mode=False,
             gateway='mock'):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    if stripe:
        ledger.add_gateway(_DecliningStripe())
    if dev_mode:
        monkeypatch.setenv('HEVOLVE_DEV_MODE', 'true')
    else:
        monkeypatch.delenv('HEVOLVE_DEV_MODE', raising=False)
    user = types.SimpleNamespace(id='u-9', is_admin=is_admin)
    api_key = types.SimpleNamespace(id='k1', tier='free', rate_limit_per_day=100, monthly_quota=3000)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = api_key
    body = {'target_tier': 'enterprise', 'payment_method': 'anything'}
    if gateway:
        body['gateway'] = gateway
    with patch('integrations.social.auth._get_user_from_token', return_value=(user, db)), \
            patch('integrations.ap2.payment_ledger', ledger):
        r = client.post('/api/v1/intelligence/keys/k1/upgrade', json=body,
                        headers={'Authorization': 'Bearer tok'})
    return r, api_key, db, ledger


@pytest.mark.parametrize('gateway', ['mock', ' MOCK ', None])
def test_a_person_cannot_upgrade_through_the_mock_when_no_live_gateway_exists(
        client, tmp_path, monkeypatch, gateway):
    r, api_key, db, ledger = _upgrade(client, tmp_path, monkeypatch, gateway=gateway)
    assert r.status_code == 503, r.get_json()
    assert 'STRIPE_API_KEY' in r.get_json()['error']
    assert api_key.tier == 'free'
    db.commit.assert_not_called()
    assert ledger.payments == {}                       # nothing recorded as paid


def test_a_person_naming_the_mock_still_pays_through_stripe(client, tmp_path, monkeypatch):
    r, api_key, db, ledger = _upgrade(client, tmp_path, monkeypatch, stripe=True, gateway='mock')
    assert r.status_code == 402, r.get_json()
    assert api_key.tier == 'free'
    db.commit.assert_not_called()
    (payment,) = ledger.payments.values()
    assert payment.gateway == PaymentGateway.STRIPE   # the card was asked, and declined


def test_an_operator_may_settle_through_the_mock(client, tmp_path, monkeypatch):
    r, api_key, db, ledger = _upgrade(client, tmp_path, monkeypatch, is_admin=True)
    assert r.status_code == 200, r.get_json()
    assert api_key.tier == 'enterprise'
    db.commit.assert_called_once()


def test_dev_mode_lets_a_dev_node_settle_through_the_mock(client, tmp_path, monkeypatch):
    r, api_key, db, ledger = _upgrade(client, tmp_path, monkeypatch, dev_mode=True)
    assert r.status_code == 200, r.get_json()
    assert api_key.tier == 'enterprise'


def test_phonepe_is_still_refused_on_the_synchronous_route(client, tmp_path, monkeypatch):
    r, api_key, db, ledger = _upgrade(client, tmp_path, monkeypatch, gateway='phonepe')
    assert r.status_code == 501
    assert api_key.tier == 'free' and ledger.payments == {}
