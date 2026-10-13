"""A paid Stripe Checkout bumps the tier through Stripe's signed webhook
(task #122, Stripe half).

Before: the tier moved only if the buyer's browser reached the success page
and that page POSTed the session id to /upgrade/checkout/complete; the
deployed landing page has no such page, so a buyer was charged and never
upgraded.  Now Stripe's checkout.session.completed event lands on
/api/v1/intelligence/stripe/webhook, is verified against
STRIPE_WEBHOOK_SECRET and finalized by the same code the browser route
uses; the two may finish the same session and only one records it.

Real blueprint, real PaymentLedger and StripePaymentGateway; the Stripe SDK
handle (the wire to Stripe) and the database session are stubbed.
"""
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import integrations.ap2 as ap2_pkg
from integrations.ap2.ap2_protocol import (
    PaymentGateway, PaymentLedger, PaymentStatus, StripePaymentGateway,
)

WEBHOOK = '/api/v1/intelligence/stripe/webhook'
COMPLETE = '/api/v1/intelligence/upgrade/checkout/complete'
SECRET = 'whsec_test_1'
SIGNATURE = 't=1,v1=deadbeef'
RAW = b'{"id": "evt_1", "type": "checkout.session.completed"}'


class _SdkObject:
    """What the stripe SDK hands back from 15.0.0 on: attribute access and
    to_dict(), and NOT a dict (no .get; the review of 3153761b3 measured a
    500 on every paid event with 15.0.0 and 16.0.0, where 7.0.0 through
    14.4.1 still subclass dict)."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def to_dict(self):
        return dict(self.__dict__)


def _session(**over):
    base = dict(id='cs_test_1', payment_status='paid', amount_total=4900,
                currency='usd', payment_intent='pi_1', customer=None,
                metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': 'k1',
                          'user_id': 'u-1', 'current_tier': 'free',
                          'target_tier': 'pro'})
    base.update(over)
    if isinstance(base.get('metadata'), dict):
        base['metadata'] = _SdkObject(**base['metadata'])
    return types.SimpleNamespace(**base)


def _page_fields(body):
    """What the landing page's success screen renders (UpgradeSuccess.js):
    the tier and limits, the amount, the split, the receipt ids."""
    return (body['api_key']['tier'], body['api_key']['rate_limit_per_day'],
            body['api_key']['monthly_quota'], body['payment']['amount_usd'],
            body['payment']['stripe_payment_intent'],
            body['revenue_split']['users_pool_usd'], body['payment_request_id'])


def _event(session, type_='checkout.session.completed'):
    return types.SimpleNamespace(type=type_, data=types.SimpleNamespace(object=session))


@pytest.fixture
def ctx(tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    gw = StripePaymentGateway(api_key='sk_test_x')
    gw.connected = True
    gw._stripe = MagicMock()
    gw.webhook_secret = SECRET
    ledger.gateways[PaymentGateway.STRIPE] = gw
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    api_key = types.SimpleNamespace(id='k1', tier='free', rate_limit_per_day=100,
                                    monthly_quota=3000)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = api_key
    with patch.object(ap2_pkg, 'payment_ledger', ledger), \
            patch('integrations.social.models.get_db', return_value=db):
        yield {'client': app.test_client(), 'ledger': ledger, 'gw': gw,
               'stripe': gw._stripe, 'db': db, 'api_key': api_key}


def _deliver(ctx, event=None, raw=RAW, signature=SIGNATURE):
    if event is not None:
        ctx['stripe'].Webhook.construct_event.return_value = event
    headers = {'Stripe-Signature': signature} if signature is not None else {}
    return ctx['client'].post(WEBHOOK, data=raw, headers=headers,
                              content_type='application/json')


class TestTheWebhookFinalizesAPaidSession:
    def test_a_paid_session_bumps_the_tier_and_records_the_payment(self, ctx):
        r = _deliver(ctx, _event(_session()))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['success'] is True and body['api_key']['tier'] == 'pro'
        assert ctx['api_key'].tier == 'pro'
        assert ctx['api_key'].monthly_quota == 300000
        ctx['db'].commit.assert_called_once()
        (payment,) = ctx['ledger'].payments.values()
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.gateway == PaymentGateway.STRIPE
        assert payment.metadata['stripe_session_id'] == 'cs_test_1'
        assert str(payment.amount) == '49.0'
        ctx['stripe'].Webhook.construct_event.assert_called_once_with(RAW, SIGNATURE, SECRET)

    def test_a_redelivery_records_nothing_twice_and_answers_the_same_page(self, ctx):
        """The second answer carries what the first did (the success page
        renders the tier, the amount, the split and the receipt; the review
        of 3153761b3 ran it on the bare already_completed answer and it
        threw on api_key.tier)."""
        first = _deliver(ctx, _event(_session())).get_json()
        r = _deliver(ctx, _event(_session()))
        assert r.status_code == 200
        body = r.get_json()
        assert body['already_completed'] is True and body['success'] is True
        assert _page_fields(body) == _page_fields(first)
        assert len(ctx['ledger'].payments) == 1
        ctx['db'].commit.assert_called_once()

    def test_a_cheaper_older_session_never_lowers_the_tier(self, ctx):
        """Stripe does not order deliveries and events queue while the secret
        is missing: a starter session finalized after a pro one records its
        payment and leaves the key on pro (the review of 3153761b3 measured
        the downgrade)."""
        _deliver(ctx, _event(_session(id='cs_new_pro')))
        assert ctx['api_key'].tier == 'pro'
        r = _deliver(ctx, _event(_session(
            id='cs_old_starter', amount_total=900,
            metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': 'k1',
                      'user_id': 'u-1', 'target_tier': 'starter'})))
        assert r.status_code == 200, r.get_json()
        assert ctx['api_key'].tier == 'pro'
        assert ctx['api_key'].monthly_quota == 300000
        assert r.get_json()['api_key']['tier'] == 'pro'
        assert len(ctx['ledger'].payments) == 2

    def test_eight_completers_of_one_session_record_it_once(self, ctx):
        """The browser and the webhook may finish one session at the same
        moment: the record is made under the ledger's lock.  Without the
        lock the review of 3153761b3 measured duplicates in 117 of 150
        tight races; twenty rounds here would all have to come out clean
        by luck for a lockless finalizer to pass."""
        import sys
        import threading
        from integrations.agent_engine.commercial_api import _finalize_paid_checkout_session
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for _ in range(20):
                ctx['ledger'].payments.clear()
                ctx['api_key'].tier = 'free'
                threads = [threading.Thread(
                    target=_finalize_paid_checkout_session, args=(_session(), ctx['db']))
                    for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                assert len(ctx['ledger'].payments) == 1
                assert ctx['api_key'].tier == 'pro'
        finally:
            sys.setswitchinterval(interval)

    def test_a_bad_signature_is_refused_and_nothing_moves(self, ctx):
        ctx['stripe'].Webhook.construct_event.side_effect = Exception('No signatures found')
        r = _deliver(ctx)
        assert r.status_code == 400
        assert ctx['ledger'].payments == {}
        ctx['db'].commit.assert_not_called()
        assert ctx['api_key'].tier == 'free'

    def test_without_a_webhook_secret_nothing_is_trusted(self, ctx):
        ctx['gw'].webhook_secret = ''
        r = _deliver(ctx)
        assert r.status_code == 503
        assert 'STRIPE_WEBHOOK_SECRET' in r.get_json()['error']
        ctx['stripe'].Webhook.construct_event.assert_not_called()
        assert ctx['ledger'].payments == {}

    def test_an_unregistered_gateway_is_a_503(self, ctx):
        del ctx['ledger'].gateways[PaymentGateway.STRIPE]
        assert _deliver(ctx).status_code == 503

    def test_another_event_type_is_acknowledged_and_ignored(self, ctx):
        r = _deliver(ctx, _event(_session(), type_='payment_intent.succeeded'))
        assert r.status_code == 200 and r.get_json()['status'] == 'ignored'
        assert ctx['ledger'].payments == {}

    def test_an_unpaid_session_is_acknowledged_and_not_finalized(self, ctx):
        r = _deliver(ctx, _event(_session(payment_status='unpaid')))
        assert r.status_code == 200 and r.get_json()['status'] == 'unpaid'
        assert ctx['ledger'].payments == {}
        assert ctx['api_key'].tier == 'free'

    def test_another_checkout_on_the_account_is_ignored_without_an_alarm(self, ctx):
        """A paid session that is not a tier upgrade (another product on the
        same Stripe account, the dashboard's test event) is acknowledged
        and ignored; it is not a reconciliation case (the review of
        3153761b3: it raised the MANUAL RECONCILIATION alarm)."""
        import logging
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session(metadata={'kind': 'something_else'})))
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True and body['status'] == 'ignored'
        said.assert_not_called()
        assert ctx['ledger'].payments == {}
        assert ctx['api_key'].tier == 'free'

    def test_a_tier_upgrade_whose_key_is_gone_is_named_for_a_person(self, ctx):
        """A retry cannot fix a missing key: Stripe is answered 200 so it
        stops, the operator is told at ERROR, nothing is recorded."""
        import logging
        ctx['db'].query.return_value.filter_by.return_value.first.return_value = None
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session()))
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is False and body['status'] == 'not_finalized'
        assert said.called and 'cs_test_1' in said.call_args.args[0]
        assert ctx['ledger'].payments == {}

    def test_a_tier_bump_that_fails_is_finished_by_the_retry(self, ctx):
        """The record is made before the bump, so a failed bump leaves a
        recorded, unbumped session; Stripe's retry (a 500 asks for one)
        finds the record and finishes the bump instead of answering
        already_completed over a key still on the old tier (the review of
        3153761b3 measured that answer)."""
        ctx['db'].commit.side_effect = [RuntimeError('db locked'), None]
        r = _deliver(ctx, _event(_session()))
        assert r.status_code == 500
        assert ctx['api_key'].tier == 'free'
        r2 = _deliver(ctx, _event(_session()))
        assert r2.status_code == 200, r2.get_json()
        assert ctx['api_key'].tier == 'pro'
        assert r2.get_json()['api_key']['tier'] == 'pro'
        assert len(ctx['ledger'].payments) == 1
        assert ctx['db'].commit.call_count == 2


def _signed_in(db):
    user = types.SimpleNamespace(id='u-1', email=None)
    return patch('integrations.social.auth._get_user_from_token', return_value=(user, db))


class TestTheBrowserRouteSharesTheFinalizer:
    def test_the_success_page_completion_still_bumps_and_the_webhook_then_defers(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session()
        with _signed_in(ctx['db']):
            r = ctx['client'].post(COMPLETE, json={'session_id': 'cs_test_1'},
                                   headers={'Authorization': 'Bearer tok'})
        assert r.status_code == 200, r.get_json()
        assert ctx['api_key'].tier == 'pro'
        r2 = _deliver(ctx, _event(_session()))
        assert r2.status_code == 200 and r2.get_json()['already_completed'] is True
        assert len(ctx['ledger'].payments) == 1

    def test_the_webhook_first_then_the_success_page_defers_without_asking_stripe(self, ctx):
        first = _deliver(ctx, _event(_session())).get_json()
        with _signed_in(ctx['db']):
            r = ctx['client'].post(COMPLETE, json={'session_id': 'cs_test_1'},
                                   headers={'Authorization': 'Bearer tok'})
        assert r.status_code == 200 and r.get_json()['already_completed'] is True
        assert _page_fields(r.get_json()) == _page_fields(first)
        ctx['stripe'].checkout.Session.retrieve.assert_not_called()
        assert len(ctx['ledger'].payments) == 1

    def test_a_session_without_metadata_is_not_a_tier_upgrade(self, ctx):
        """The kind is checked before the user, as before the finalizer was
        lifted out: no metadata is a 400, not 'belongs to a different user'
        (the review of 3153761b3 measured the reorder)."""
        ctx['stripe'].checkout.Session.retrieve.return_value = _session(metadata=None)
        with _signed_in(ctx['db']):
            r = ctx['client'].post(COMPLETE, json={'session_id': 'cs_test_1'},
                                   headers={'Authorization': 'Bearer tok'})
        assert r.status_code == 400
        assert r.get_json()['error'] == 'Session is not a tier upgrade.'

    def test_another_persons_session_is_refused_by_the_browser_route(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session(
            metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': 'k1',
                      'user_id': 'u-2', 'target_tier': 'pro'})
        with _signed_in(ctx['db']):
            r = ctx['client'].post(COMPLETE, json={'session_id': 'cs_test_1'},
                                   headers={'Authorization': 'Bearer tok'})
        assert r.status_code == 403
        assert ctx['ledger'].payments == {} and ctx['api_key'].tier == 'free'


class TestVerifyWebhook:
    def test_the_sdk_is_asked_with_the_secret(self):
        gw = StripePaymentGateway(api_key='sk_test_x')
        gw.connected, gw._stripe, gw.webhook_secret = True, MagicMock(), SECRET
        gw._stripe.Webhook.construct_event.return_value = 'the event'
        assert gw.verify_webhook(RAW, SIGNATURE) == 'the event'
        gw._stripe.Webhook.construct_event.assert_called_once_with(RAW, SIGNATURE, SECRET)

    def test_a_rejected_signature_is_a_value_error(self):
        gw = StripePaymentGateway(api_key='sk_test_x')
        gw.connected, gw._stripe, gw.webhook_secret = True, MagicMock(), SECRET
        gw._stripe.Webhook.construct_event.side_effect = Exception('bad')
        with pytest.raises(ValueError):
            gw.verify_webhook(RAW, SIGNATURE)

    def test_no_secret_or_no_connection_never_verifies(self):
        gw = StripePaymentGateway(api_key='sk_test_x')
        gw.connected, gw._stripe, gw.webhook_secret = True, MagicMock(), ''
        with pytest.raises(ValueError):
            gw.verify_webhook(RAW, SIGNATURE)
        gw.connected, gw.webhook_secret = False, SECRET
        with pytest.raises(ValueError):
            gw.verify_webhook(RAW, SIGNATURE)
        gw._stripe.Webhook.construct_event.assert_not_called()

    def test_the_secret_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', 'whsec_env')
        assert StripePaymentGateway(api_key='sk_test_x').webhook_secret == 'whsec_env'
        monkeypatch.delenv('STRIPE_WEBHOOK_SECRET')
        assert StripePaymentGateway(api_key='sk_test_x').webhook_secret == ''
