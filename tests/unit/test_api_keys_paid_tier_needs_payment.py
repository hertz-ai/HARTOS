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
import logging
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
    PaymentGateway, PaymentGatewayConnector, PaymentLedger, PaymentStatus,
)
from tests.unit.commercial_db import KEY, OTHER, OWNER, key_db  # noqa: E402,F401  (a fixture)


class _DecliningStripe(PaymentGatewayConnector):
    """A registered Stripe connector whose card is declined."""

    def __init__(self):
        super().__init__(PaymentGateway.STRIPE)

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        return {'success': False, 'error': 'card_declined'}


def _as_owner(key_db, is_admin=False):
    """The key's owner signed in, handed a session of the real table (the
    route reads the key by id AND user, and writes its tier through one
    conditional UPDATE; a MagicMock session answered neither)."""
    user = types.SimpleNamespace(id=OWNER, is_admin=is_admin)
    return patch('integrations.social.auth._get_user_from_token',
                 side_effect=lambda tok: (user, key_db.session()))


def _upgrade(client, key_db, tmp_path, monkeypatch, *, is_admin=False, stripe=False,
             dev_mode=False, gateway='mock'):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    if stripe:
        ledger.add_gateway(_DecliningStripe())
    if dev_mode:
        monkeypatch.setenv('HEVOLVE_DEV_MODE', 'true')
    else:
        monkeypatch.delenv('HEVOLVE_DEV_MODE', raising=False)
    body = {'target_tier': 'enterprise', 'payment_method': 'anything'}
    if gateway:
        body['gateway'] = gateway
    with _as_owner(key_db, is_admin), patch('integrations.ap2.payment_ledger', ledger):
        r = client.post(f'/api/v1/intelligence/keys/{KEY}/upgrade', json=body,
                        headers={'Authorization': 'Bearer tok'})
    return r, ledger


def _tier_of(key_db):
    return key_db.key()[0]


@pytest.mark.parametrize('gateway', ['mock', ' MOCK ', None])
def test_a_person_cannot_upgrade_through_the_mock_when_no_live_gateway_exists(
        client, key_db, tmp_path, monkeypatch, gateway, caplog):
    with caplog.at_level(logging.WARNING, logger='integrations.ap2.ap2_protocol'):
        r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch, gateway=gateway)
    assert r.status_code == 503, r.get_json()
    # The one refusal wording for the customer (no setting named), and the
    # one operator WARNING that names it (review of 3a07d3989, F3).
    assert 'no live payment gateway is configured on this node' in r.get_json()['error']
    assert 'STRIPE_API_KEY' not in r.get_json()['error']
    warned = [rec.getMessage() for rec in caplog.records
              if rec.name == 'integrations.ap2.ap2_protocol' and rec.levelno == logging.WARNING]
    assert len(warned) == 1 and 'STRIPE_API_KEY' in warned[0] and '=' not in warned[0]
    assert _tier_of(key_db) == 'free'
    assert ledger.payments == {}                       # nothing recorded as paid


def test_a_person_naming_the_mock_still_pays_through_stripe(client, key_db, tmp_path, monkeypatch):
    r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch, stripe=True, gateway='mock')
    assert r.status_code == 402, r.get_json()
    assert _tier_of(key_db) == 'free'
    (payment,) = ledger.payments.values()
    assert payment.gateway == PaymentGateway.STRIPE   # the card was asked, and declined


class _AuthorizingStripe(PaymentGatewayConnector):
    """A registered Stripe connector whose card is charged and captured."""

    def __init__(self):
        super().__init__(PaymentGateway.STRIPE)

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        return {'success': True, 'transaction_id': 'ch_test_1', 'status': 'authorized'}

    def capture_payment(self, payment_id, gateway_transaction_id):
        return {'success': True, 'transaction_id': gateway_transaction_id, 'status': 'captured'}


def test_a_person_who_pays_through_stripe_gets_the_tier(client, key_db, tmp_path, monkeypatch):
    """Review of b6502ac00, mutant M13: nothing pinned the honest path.  A
    refusal after a successful charge would take the money and keep the
    tier."""
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    ledger.add_gateway(_AuthorizingStripe())
    monkeypatch.delenv('HEVOLVE_DEV_MODE', raising=False)
    with _as_owner(key_db), patch('integrations.ap2.payment_ledger', ledger):
        r = client.post(f'/api/v1/intelligence/keys/{KEY}/upgrade',
                        json={'target_tier': 'pro', 'payment_method': 'pm_card_visa'},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert _tier_of(key_db) == 'pro' and body['api_key']['tier'] == 'pro'
    assert body['api_key']['monthly_quota'] == key_db.key()[2]
    assert body['payment']['status'] == 'completed' and 'revenue_split' in body
    (payment,) = ledger.payments.values()
    assert payment.gateway == PaymentGateway.STRIPE


def test_a_charge_whose_tier_bump_fails_asks_for_a_person_now(client, key_db, tmp_path,
                                                                monkeypatch):
    """The card was charged and the tier write failed: 500 with status
    bump_failed, the payment recorded as taken, and ONE ERROR line that
    names the payment and says no retry follows, since a re-POST of
    /upgrade would charge the card again; so it asks for a person now, not
    'if this repeats' (the review of 7c8431c2e: one wording claimed the
    gateway's redelivery or the buyer's reload on every rail)."""
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    ledger.add_gateway(_AuthorizingStripe())
    monkeypatch.delenv('HEVOLVE_DEV_MODE', raising=False)
    key_db.fail_next_commit()
    log = logging.getLogger('hevolve_social')
    with _as_owner(key_db), patch('integrations.ap2.payment_ledger', ledger), \
            patch.object(log, 'error') as said, patch.object(log, 'exception') as traced:
        r = client.post(f'/api/v1/intelligence/keys/{KEY}/upgrade',
                        json={'target_tier': 'pro', 'payment_method': 'pm_card_visa'},
                        headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 500, r.get_json()
    body = r.get_json()
    assert body['success'] is False and body['status'] == 'bump_failed'
    (payment,) = ledger.payments.values()
    assert body['payment_request_id'] == payment.payment_id
    assert payment.status == PaymentStatus.COMPLETED          # the money was taken
    traced.assert_not_called()
    assert said.call_count == 1
    line = said.call_args.args[0]
    assert payment.payment_id in line and 'charge the card again' in line
    assert 'if this repeats' not in line
    assert _tier_of(key_db) == 'free'


def test_the_mock_gate_reads_the_one_dev_mode_switch(client, key_db, tmp_path, monkeypatch):
    """security.master_key.is_dev_mode is the switch the security layer reads;
    the money gate must read the same one, not its own copy of the env rule."""
    from security import master_key
    monkeypatch.delenv('HEVOLVE_DEV_MODE', raising=False)
    monkeypatch.setattr(master_key, 'is_dev_mode', lambda: True)
    r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch)
    assert r.status_code == 200 and _tier_of(key_db) == 'enterprise'


def test_an_operator_may_settle_through_the_mock(client, key_db, tmp_path, monkeypatch):
    r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch, is_admin=True)
    assert r.status_code == 200, r.get_json()
    assert _tier_of(key_db) == 'enterprise'


def test_dev_mode_lets_a_dev_node_settle_through_the_mock(client, key_db, tmp_path, monkeypatch):
    r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch, dev_mode=True)
    assert r.status_code == 200, r.get_json()
    assert _tier_of(key_db) == 'enterprise'


def test_phonepe_is_still_refused_on_the_synchronous_route(client, key_db, tmp_path, monkeypatch):
    r, ledger = _upgrade(client, key_db, tmp_path, monkeypatch, gateway='phonepe')
    assert r.status_code == 501
    assert _tier_of(key_db) == 'free' and ledger.payments == {}


# ── revoking is the owner's ─────────────────────────────────────────────

def _revoke_as(client, key_db, user_id):
    user = types.SimpleNamespace(id=user_id, is_admin=False)
    with patch('integrations.social.auth._get_user_from_token',
               side_effect=lambda tok: (user, key_db.session())):
        return client.delete(f'/api/v1/intelligence/keys/{KEY}',
                             headers={'Authorization': 'Bearer tok'})


def test_another_person_cannot_revoke_a_key_they_do_not_own(client, key_db):
    """DELETE /keys/<id> deactivated any key by id: every other key route
    reads the key by id AND the JWT's user, this one did not, so a signed-in
    person who knew another's key id could switch it off (the review of
    fd2b4773c read it; task #146).  The other person gets the same 404 a
    missing key gets, and the key stays active."""
    r = _revoke_as(client, key_db, OTHER)
    assert r.status_code == 404, r.get_json()
    assert key_db.is_active() is True


def test_the_owner_revokes_their_own_key(client, key_db):
    r = _revoke_as(client, key_db, OWNER)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['key']['is_active'] is False
    assert key_db.is_active() is False
