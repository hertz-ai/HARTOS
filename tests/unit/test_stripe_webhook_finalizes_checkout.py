"""A paid Stripe Checkout bumps the tier through Stripe's signed webhook
(task #122, Stripe half), and only an upgrade is ever sold.

Before: the tier moved only if the buyer's browser reached the success page
and that page POSTed the session id to /upgrade/checkout/complete; the
deployed landing page has no such page, so a buyer was charged and never
upgraded.  Now Stripe's checkout.session.completed event lands on
/api/v1/intelligence/stripe/webhook, is verified against
STRIPE_WEBHOOK_SECRET and finalized by the same code the browser route
uses; the two may finish the same session and only one records it.  The
tier is written by ONE conditional UPDATE (never lower, whatever order two
deliveries land in), a non-upgrade is refused before anything is charged,
and the browser's fast path finishes a bump the webhook could not.

Real blueprint, real PaymentLedger and StripePaymentGateway, a real
api_keys table (tests/unit/commercial_db); the Stripe SDK handle (the wire
to Stripe) is stubbed.
"""
import contextlib
import logging
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from sqlalchemy import event

import integrations.ap2 as ap2_pkg
from integrations.ap2.ap2_protocol import (
    PaymentGateway, PaymentLedger, PaymentStatus, StripePaymentGateway,
)
from tests.unit.commercial_db import (  # noqa: F401  (key_db is a fixture)
    KEY, OTHER, OWNER, key_db, tier_tuple,
)

WEBHOOK = '/api/v1/intelligence/stripe/webhook'
COMPLETE = '/api/v1/intelligence/upgrade/checkout/complete'
CHECKOUT = f'/api/v1/intelligence/keys/{KEY}/upgrade/checkout'
UPGRADE = f'/api/v1/intelligence/keys/{KEY}/upgrade'
PHONEPE_UPGRADE = f'/api/v1/intelligence/keys/{KEY}/upgrade/phonepe'
SECRET = 'whsec_test_1'
SIGNATURE = 't=1,v1=deadbeef'
RAW = b'{"id": "evt_1", "type": "checkout.session.completed"}'
AUTH = {'Authorization': 'Bearer tok'}


FREE, STARTER, PRO = tier_tuple('free'), tier_tuple('starter'), tier_tuple('pro')


class _SdkObject:
    """What the stripe SDK hands back from 15.0.0 on: attribute access and
    to_dict(), and NOT a dict (no .get; the review of 3153761b3 measured a
    500 on every paid event with 15.0.0 and 16.0.0, where 7.0.0 through
    14.4.1 still subclass dict)."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def to_dict(self):
        return dict(self.__dict__)


class _UnreadableMetadata:
    """A metadata object whose to_dict raises: the next SDK shape change."""

    def to_dict(self):
        raise RuntimeError('the SDK changed shape again')


def _session(**over):
    base = dict(id='cs_test_1', payment_status='paid', amount_total=4900,
                currency='usd', payment_intent='pi_1', customer=None,
                metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': KEY,
                          'user_id': OWNER, 'current_tier': 'free',
                          'target_tier': 'pro'})
    base.update(over)
    if isinstance(base.get('metadata'), dict):
        base['metadata'] = _SdkObject(**base['metadata'])
    return types.SimpleNamespace(**base)


def _starter_session(id='cs_starter'):
    return _session(id=id, amount_total=900,
                    metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': KEY,
                              'user_id': OWNER, 'target_tier': 'starter'})


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
def ctx(tmp_path, key_db):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    gw = StripePaymentGateway(api_key='sk_test_x')
    gw.connected = True
    gw._stripe = MagicMock()
    gw.webhook_secret = SECRET
    ledger.gateways[PaymentGateway.STRIPE] = gw
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with patch.object(ap2_pkg, 'payment_ledger', ledger):
        yield {'client': app.test_client(), 'ledger': ledger, 'gw': gw,
               'stripe': gw._stripe, 'db': key_db}


def _deliver(ctx, event=None, raw=RAW, signature=SIGNATURE):
    if event is not None:
        ctx['stripe'].Webhook.construct_event.return_value = event
    headers = {'Stripe-Signature': signature} if signature is not None else {}
    return ctx['client'].post(WEBHOOK, data=raw, headers=headers,
                              content_type='application/json')


def _signed_in(ctx, user_id=OWNER):
    """The JWT's person and the session the route is handed (one per
    request, as _get_user_from_token opens it)."""
    user = types.SimpleNamespace(id=user_id, email=None)
    return patch('integrations.social.auth._get_user_from_token',
                 side_effect=lambda tok: (user, ctx['db'].session()))


def _complete(ctx, session_id='cs_test_1', user_id=OWNER):
    with _signed_in(ctx, user_id):
        return ctx['client'].post(COMPLETE, json={'session_id': session_id}, headers=AUTH)


def _finalize(session, db):
    from integrations.agent_engine.commercial_api import _finalize_paid_checkout_session
    s = db.session()
    try:
        return _finalize_paid_checkout_session(session, s)
    finally:
        s.close()


class TestTheWebhookFinalizesAPaidSession:
    def test_a_paid_session_bumps_the_tier_and_records_the_payment(self, ctx):
        r = _deliver(ctx, _event(_session()))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['success'] is True and body['api_key']['tier'] == 'pro'
        assert ctx['db'].key() == PRO
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
        assert ctx['db'].key() == PRO

    def test_a_cheaper_older_session_never_lowers_the_tier(self, ctx):
        """Stripe does not order deliveries and events queue while the secret
        is missing: a starter session finalized after a pro one records its
        payment and leaves the key on pro (the review of 3153761b3 measured
        the downgrade).  That is money taken for a tier the key is already
        above, a refund case, so the operator gets a WARNING naming the key
        and both tiers; a plain redelivery of an applied session gets no
        line at all (central logs nothing below WARNING, and the one INFO
        line covered both cases: the review of ec0dbe7be)."""
        log = logging.getLogger('hevolve_social')
        _deliver(ctx, _event(_session(id='cs_new_pro')))
        assert ctx['db'].key() == PRO
        with patch.object(log, 'warning') as warned, patch.object(log, 'info') as told:
            _deliver(ctx, _event(_session(id='cs_new_pro')))        # a redelivery
        warned.assert_not_called()
        told.assert_not_called()
        with patch.object(log, 'warning') as warned:
            r = _deliver(ctx, _event(_starter_session('cs_old_starter')))
        assert r.status_code == 200, r.get_json()
        assert ctx['db'].key() == PRO
        assert r.get_json()['api_key']['tier'] == 'pro'
        assert len(ctx['ledger'].payments) == 2
        assert warned.call_count == 1
        said = warned.call_args.args[0] % warned.call_args.args[1:]
        assert KEY in said and 'pro' in said and 'starter' in said

    def test_two_deliveries_racing_never_lower_the_tier(self, ctx):
        """The pro and the starter session delivered at the same moment, each
        on its own database session: the key ends on pro every time.  A
        check-then-act on a stale read (the starter delivery read 'free',
        waited on the ledger lock while the pro bump committed, then wrote
        starter) landed on starter in 16 to 23 of 40 rounds (the review of
        fd2b4773c measured it); the bought tier is now one conditional
        UPDATE that the database orders."""
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for _ in range(40):
                ctx['db'].put_key_on('free')
                ctx['ledger'].payments.clear()
                threads = [threading.Thread(target=_finalize, args=(s, ctx['db']))
                           for s in (_session(id='cs_pro'), _starter_session())]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                assert ctx['db'].key() == PRO
                assert len(ctx['ledger'].payments) == 2
        finally:
            sys.setswitchinterval(interval)

    def test_eight_completers_of_one_session_record_it_once(self, ctx):
        """The browser and the webhook may finish one session at the same
        moment: the record is made under the ledger's lock.  Without the
        lock the review of 3153761b3 measured duplicates in 117 of 150
        tight races; twenty rounds here would all have to come out clean
        by luck for a lockless finalizer to pass."""
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for _ in range(20):
                ctx['ledger'].payments.clear()
                ctx['db'].put_key_on('free')
                threads = [threading.Thread(target=_finalize, args=(_session(), ctx['db']))
                           for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                assert len(ctx['ledger'].payments) == 1
                assert ctx['db'].key() == PRO
        finally:
            sys.setswitchinterval(interval)

    def test_a_bad_signature_is_refused_and_nothing_moves(self, ctx):
        ctx['stripe'].Webhook.construct_event.side_effect = Exception('No signatures found')
        r = _deliver(ctx)
        assert r.status_code == 400
        assert ctx['ledger'].payments == {}
        assert ctx['db'].key() == FREE

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
        assert ctx['db'].key() == FREE

    def test_another_checkout_on_the_account_is_ignored_without_an_alarm(self, ctx):
        """A paid session that is not a tier upgrade (another product on the
        same Stripe account, the dashboard's test event) is acknowledged
        and ignored; it is not a reconciliation case (the review of
        3153761b3: it raised the MANUAL RECONCILIATION alarm)."""
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session(metadata={'kind': 'something_else'})))
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True and body['status'] == 'ignored'
        said.assert_not_called()
        assert ctx['ledger'].payments == {}
        assert ctx['db'].key() == FREE

    def test_metadata_this_node_cannot_read_is_not_acknowledged(self, ctx):
        """Readable and not ours is ignored (200, Stripe stops); unreadable
        is a 500 with an ERROR line, so Stripe keeps delivering until a
        person has read it.  The reader used to turn a to_dict that raised
        into {} and the webhook answered 200 ignored at INFO, which is the
        round-one critical (a paid event lost) with the retries stopped
        (the review of fd2b4773c measured it)."""
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session(metadata=_UnreadableMetadata())))
        assert r.status_code == 500, r.get_json()
        assert r.get_json()['success'] is False
        assert said.called and 'cs_test_1' in said.call_args.args[0] % said.call_args.args[1:]
        assert ctx['ledger'].payments == {}
        assert ctx['db'].key() == FREE

    def test_a_tier_upgrade_whose_key_is_gone_is_named_for_a_person(self, ctx):
        """A retry cannot fix a missing key: Stripe is answered 200 so it
        stops, the operator is told at ERROR, nothing is recorded."""
        log = logging.getLogger('hevolve_social')
        gone = _session(metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': 'k-gone',
                                  'user_id': OWNER, 'target_tier': 'pro'})
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(gone))
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
        ctx['db'].fail_next_commit()
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session()))
        assert r.status_code == 500 and r.get_json()['status'] == 'bump_failed'
        assert ctx['db'].key() == FREE
        # This rail's own retry sentence (the review of 7c8431c2e): Stripe
        # redelivers, the buyer's reload re-POSTs; one line, asking for a
        # person only if it repeats.
        assert said.call_count == 1
        line = said.call_args.args[0]
        assert 'cs_test_1' in line and 'if this repeats' in line
        assert 'Stripe redelivers the event' in line
        r2 = _deliver(ctx, _event(_session()))
        assert r2.status_code == 200, r2.get_json()
        assert ctx['db'].key() == PRO
        assert r2.get_json()['api_key']['tier'] == 'pro'
        assert len(ctx['ledger'].payments) == 1


class TestTheBrowserRouteSharesTheFinalizer:
    def test_the_success_page_completion_still_bumps_and_the_webhook_then_defers(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session()
        r = _complete(ctx)
        assert r.status_code == 200, r.get_json()
        assert ctx['db'].key() == PRO
        r2 = _deliver(ctx, _event(_session()))
        assert r2.status_code == 200 and r2.get_json()['already_completed'] is True
        assert len(ctx['ledger'].payments) == 1

    def test_the_webhook_first_then_the_success_page_defers_without_asking_stripe(self, ctx):
        first = _deliver(ctx, _event(_session())).get_json()
        r = _complete(ctx)
        assert r.status_code == 200 and r.get_json()['already_completed'] is True
        assert _page_fields(r.get_json()) == _page_fields(first)
        ctx['stripe'].checkout.Session.retrieve.assert_not_called()
        assert len(ctx['ledger'].payments) == 1

    def test_a_recorded_session_is_answered_to_its_owner_only(self, ctx):
        """The fast path reads the key by id AND the JWT's user: another
        signed-in person asking with the owner's session id gets 403 and no
        page (the review of fd2b4773c dropped the user filter and twenty
        tests on a MagicMock session still passed)."""
        _deliver(ctx, _event(_session()))
        r = _complete(ctx, user_id=OTHER)
        assert r.status_code == 403, r.get_json()
        assert 'api_key' not in r.get_json()
        assert _complete(ctx).status_code == 200

    def test_the_fast_path_finishes_a_bump_the_record_outlived(self, ctx):
        """The buyer's first visit records the payment and the bump fails
        (500); every reload then found the record and answered success over
        a key still on free, with "Welcome to the FREE tier" after a $49
        charge, until a webhook delivery ran the finalizer (the review of
        fd2b4773c measured it).  The fast path applies the bought tier the
        same way, and answers no success while the key sits below it."""
        ctx['stripe'].checkout.Session.retrieve.return_value = _session()
        ctx['db'].fail_next_commit()
        r = _complete(ctx)
        assert r.status_code == 500 and ctx['db'].key() == FREE
        assert len(ctx['ledger'].payments) == 1
        ctx['db'].fail_next_commit()
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r2 = _complete(ctx)                   # the reload, bump fails again
        assert r2.status_code == 500 and r2.get_json()['success'] is False
        assert r2.get_json()['status'] == 'bump_failed'
        # The fast path's own retry sentence (the review of 7c8431c2e).
        assert said.call_count == 1
        line = said.call_args.args[0]
        assert 'cs_test_1' in line and 'if this repeats' in line
        assert 're-POSTs the session' in line
        assert ctx['db'].key() == FREE
        r3 = _complete(ctx)                       # the next reload finishes it
        assert r3.status_code == 200, r3.get_json()
        assert r3.get_json()['api_key']['tier'] == 'pro'
        assert ctx['db'].key() == PRO
        assert len(ctx['ledger'].payments) == 1
        ctx['stripe'].checkout.Session.retrieve.assert_called_once()

    def test_a_step_added_to_the_finish_runs_on_every_path(self, ctx, monkeypatch):
        """One finish for a recorded session (parallel-path audit 2026-10-27,
        item 6): the webhook, its redelivery and the buyer's reload of a
        session the webhook already recorded all end in
        _finish_recorded_checkout, so a step added there later runs on each.
        The reload's fast path ran its own copy of the tier, bump and
        payload sequence, where a step added to the finalizer was skipped."""
        from integrations.agent_engine import commercial_api
        real = getattr(commercial_api, '_finish_recorded_checkout', None)

        def with_a_later_step(*args, **kwargs):
            payload, status = real(*args, **kwargs)
            return dict(payload, a_later_step=True), status

        monkeypatch.setattr(commercial_api, '_finish_recorded_checkout',
                            with_a_later_step, raising=False)
        first = _deliver(ctx, _event(_session()))        # the webhook records it
        again = _deliver(ctx, _event(_session()))        # Stripe's redelivery
        reload = _complete(ctx)                          # the buyer's reload
        for r in (first, again, reload):
            assert r.status_code == 200, r.get_json()
            assert r.get_json().get('a_later_step') is True, r.get_json()
        ctx['stripe'].checkout.Session.retrieve.assert_not_called()

    def test_a_record_without_a_bought_tier_is_a_reconciliation_case(self, ctx):
        """A ledger record missing target_tier (only a hand-edited ledger can
        lack it: every writer stamps it) used to raise KeyError outside the
        writer's try and answer Flask's HTML 500 with no reconciliation line
        (the review of ec0dbe7be).  It is the same case as a failed bump."""
        _deliver(ctx, _event(_session()))
        (payment,) = ctx['ledger'].payments.values()
        del payment.metadata['target_tier']
        log = logging.getLogger('hevolve_social')
        with patch.object(log, 'error') as said:
            r = _complete(ctx)
        assert r.status_code == 500 and r.get_json()['success'] is False
        assert said.called and KEY in said.call_args.args[0]
        assert ctx['db'].key() == PRO

    def test_a_session_without_metadata_is_not_a_tier_upgrade(self, ctx):
        """The kind is checked before the user, as before the finalizer was
        lifted out: no metadata is a 400, not 'belongs to a different user'
        (the review of 3153761b3 measured the reorder)."""
        ctx['stripe'].checkout.Session.retrieve.return_value = _session(metadata=None)
        r = _complete(ctx)
        assert r.status_code == 400
        assert r.get_json()['error'] == 'Session is not a tier upgrade.'

    def test_metadata_this_node_cannot_read_is_a_500_here_too(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session(
            metadata=_UnreadableMetadata())
        r = _complete(ctx)
        assert r.status_code == 500 and r.get_json()['success'] is False
        assert ctx['ledger'].payments == {} and ctx['db'].key() == FREE

    def test_another_persons_session_is_refused_by_the_browser_route(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session(
            metadata={'kind': 'tier_upgrade_checkout', 'api_key_id': KEY,
                      'user_id': OTHER, 'target_tier': 'pro'})
        r = _complete(ctx)
        assert r.status_code == 403
        assert ctx['ledger'].payments == {} and ctx['db'].key() == FREE


@contextlib.contextmanager
def _database_gone_after_the_failed_bump(db):
    """The bump's commit fails and the database stays unreachable from then
    on (a lost connection, not a locked file): every statement after the
    failed commit raises.  The rollback the bump runs is not a statement."""
    gone = {'now': False}

    def fail_the_commit(session):
        gone['now'] = True
        raise RuntimeError('database connection lost')

    def refuse_every_statement(conn, cursor, statement, params, context, executemany):
        if gone['now']:
            raise RuntimeError('database unreachable')

    event.listen(db.Session, 'before_commit', fail_the_commit)
    event.listen(db.engine, 'before_cursor_execute', refuse_every_statement)
    try:
        yield
    finally:
        event.remove(db.Session, 'before_commit', fail_the_commit)
        event.remove(db.engine, 'before_cursor_execute', refuse_every_statement)


class TestAFailedBumpIsNamedWithTheDatabaseGone:
    """A failed bump rolls the session back, which expires the key row; the
    finish read api_key.id after that, so with the database gone the read
    raised inside the failure report: Flask's HTML 500, no reconciliation
    line naming the session and the key, and "HTTP 500" on the buyer's
    success page (the review of 2971c00db measured it on all three paths;
    its parent's reload did the same).  Stripe's retry and the buyer's
    reload still finish the bump once the database is back."""

    def _assert_named(self, r, said):
        assert r.status_code == 500
        body = r.get_json(silent=True)
        assert body is not None, r.data[:200]
        assert body['status'] == 'bump_failed' and body['success'] is False
        assert said.call_count == 1
        line = said.call_args.args[0]
        assert 'MANUAL RECONCILIATION NEEDED' in line
        assert 'cs_test_1' in line and f'api_key {KEY}' in line

    def test_the_webhook_names_it(self, ctx):
        log = logging.getLogger('hevolve_social')
        with _database_gone_after_the_failed_bump(ctx['db']), \
                patch.object(log, 'error') as said:
            r = _deliver(ctx, _event(_session()))
        self._assert_named(r, said)
        assert ctx['db'].key() == FREE and len(ctx['ledger'].payments) == 1
        again = _deliver(ctx, _event(_session()))        # Stripe's retry
        assert again.status_code == 200 and ctx['db'].key() == PRO

    def test_the_buyers_first_visit_names_it(self, ctx):
        ctx['stripe'].checkout.Session.retrieve.return_value = _session()
        log = logging.getLogger('hevolve_social')
        with _database_gone_after_the_failed_bump(ctx['db']), \
                patch.object(log, 'error') as said:
            r = _complete(ctx)
        self._assert_named(r, said)
        assert ctx['db'].key() == FREE and len(ctx['ledger'].payments) == 1
        reload = _complete(ctx)
        assert reload.status_code == 200 and ctx['db'].key() == PRO

    def test_the_buyers_reload_names_it(self, ctx):
        ctx['db'].fail_next_commit()
        assert _deliver(ctx, _event(_session())).status_code == 500   # recorded, unbumped
        log = logging.getLogger('hevolve_social')
        with _database_gone_after_the_failed_bump(ctx['db']), \
                patch.object(log, 'error') as said:
            r = _complete(ctx)
        self._assert_named(r, said)
        assert ctx['db'].key() == FREE and len(ctx['ledger'].payments) == 1
        reload = _complete(ctx)
        assert reload.status_code == 200 and ctx['db'].key() == PRO
        ctx['stripe'].checkout.Session.retrieve.assert_not_called()


class TestOnlyAnUpgradeIsSold:
    """One rule for the three rails: the tier bought must cost more than
    the one the key is on.  There is no downgrade route (no writer lowers
    a tier, revoke only deactivates), so a Pro user clicking "Choose
    starter" on the pricing page, which offers every paid tier whatever the
    key holds, was charged $9 and nothing changed; before fd2b4773c the
    same purchase lowered the key (the review of fd2b4773c measured both)."""

    def test_a_card_checkout_for_a_tier_at_or_below_the_keys_is_refused(self, ctx):
        ctx['db'].put_key_on('pro')
        for target in ('starter', 'pro'):
            with _signed_in(ctx):
                r = ctx['client'].post(CHECKOUT, json={'target_tier': target}, headers=AUTH)
            assert r.status_code == 400, (target, r.get_json())
            assert 'pro' in r.get_json()['error']
        ctx['stripe'].checkout.Session.create.assert_not_called()
        # A real upgrade still goes to Stripe.
        ctx['stripe'].checkout.Session.create.return_value = types.SimpleNamespace(
            url='https://checkout.stripe.com/c/x', id='cs_ent')
        with _signed_in(ctx):
            r = ctx['client'].post(CHECKOUT, json={'target_tier': 'enterprise'}, headers=AUTH)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['session_id'] == 'cs_ent'
        assert ctx['db'].key() == PRO

    def test_the_synchronous_upgrade_refuses_a_non_upgrade_before_charging(self, ctx):
        ctx['db'].put_key_on('pro')
        with _signed_in(ctx):
            r = ctx['client'].post(UPGRADE, json={'target_tier': 'starter',
                                                  'payment_method': 'pm_card_visa'},
                                   headers=AUTH)
        assert r.status_code == 400, r.get_json()
        assert ctx['ledger'].payments == {}
        assert ctx['db'].key() == PRO

    def test_the_phonepe_initiation_refuses_a_non_upgrade_before_any_gateway(self, ctx):
        """The rule comes before the gateway lookup: this node has no PhonePe
        gateway, and a free key buying starter still gets the 503 for that,
        where a pro key buying starter gets the 400."""
        with _signed_in(ctx):
            r = ctx['client'].post(PHONEPE_UPGRADE, json={'target_tier': 'starter'}, headers=AUTH)
        assert r.status_code == 503, r.get_json()
        ctx['db'].put_key_on('pro')
        with _signed_in(ctx):
            r = ctx['client'].post(PHONEPE_UPGRADE, json={'target_tier': 'starter'}, headers=AUTH)
        assert r.status_code == 400, r.get_json()
        assert ctx['ledger'].payments == {}

    def test_the_rule_itself(self):
        from integrations.agent_engine.commercial_api import _is_tier_upgrade
        assert _is_tier_upgrade('free', 'starter') and _is_tier_upgrade('pro', 'enterprise')
        assert _is_tier_upgrade(None, 'starter') and _is_tier_upgrade('legacy', 'starter')
        assert not _is_tier_upgrade('pro', 'pro')
        assert not _is_tier_upgrade('pro', 'starter')
        assert not _is_tier_upgrade('free', 'free')
        assert not _is_tier_upgrade('free', 'platinum')


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
