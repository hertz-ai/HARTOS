"""P1 fixes from COMMERCE_MCGROCE_FOLLOWUPS.md (items 6 and 7).

6. The generic ``process_payment`` tool must not take a payment that belongs
   to a mandate: only the settler does, after approval and a live-cart check.
7. An approval that cannot be honoured (the cart changed) must not leave the
   payment AUTHORIZED, and an expired mandate must not wait to be read.

A real PaymentLedger and MandateStore, as in test_ap2_mandate.py; time is
patched only where expiry is under test.
"""
import json
from unittest.mock import patch

import pytest

from integrations.ap2.ap2_mandate import MandateStore
from integrations.ap2.ap2_protocol import (
    PaymentLedger, PaymentStatus, create_payment_processing_function,
)

CART = {
    'lines': [{'sku_id': 11, 'qty': 2, 'unit_price': 40}],
    'total': {'amount': 80, 'currency': 'INR'},
    'currency': 'INR',
}


@pytest.fixture
def store(tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    return MandateStore(path=str(tmp_path / 'ap2_mandates.json'),
                        ledger=ledger, key=b'k' * 32)


def _approved(store):
    m = store.create_cart_mandate('u1', 'mcgroce', CART)
    assert store.approve(m.mandate_id, 'u1')[0]
    return store.get(m.mandate_id)


class TestProcessPaymentToolRefusesMandatePayments:
    def test_an_authorized_mandate_payment_is_not_charged(self, store):
        m = _approved(store)
        with patch('integrations.ap2.ap2_protocol.payment_ledger',
                   store.ledger, create=True):
            out = json.loads(create_payment_processing_function()(m.payment_id))
        assert out['success'] is False
        assert 'only by the person' in out['error']
        # untouched: still waiting for its settler
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.AUTHORIZED

    def test_a_payment_without_a_mandate_still_processes(self, store):
        p = store.ledger.create_payment_request(
            amount=5, currency='INR', description='plain',
            requester_agent_id='agent', require_approval=False)
        with patch('integrations.ap2.ap2_protocol.payment_ledger',
                   store.ledger, create=True):
            out = json.loads(create_payment_processing_function()(p.payment_id))
        # it reached the ledger: the answer is the ledger's, not the mandate refusal
        assert 'only by the person' not in out.get('error', '')


class TestWithdraw:
    def test_withdraw_rejects_the_mandate_and_cancels_the_payment(self, store):
        m = _approved(store)
        assert store.withdraw(m.mandate_id, 'cart changed after approval') is True
        assert store.get(m.mandate_id).status == 'rejected'
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_a_withdrawn_mandate_cannot_be_checked_out_again(self, store):
        m = _approved(store)
        store.withdraw(m.mandate_id, 'cart changed after approval')
        ok, reason = store.verify_for_checkout(m.mandate_id, 'u1', CART)
        assert not ok and 'rejected' in reason

    def test_only_an_approved_mandate_can_be_withdrawn(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)  # still pending
        assert store.withdraw(m.mandate_id, 'x') is False
        assert store.withdraw('missing', 'x') is False

    def test_withdraw_is_persisted(self, store):
        m = _approved(store)
        store.withdraw(m.mandate_id, 'cart changed after approval')
        raw = json.load(open(store.path))
        assert raw['mandates'][m.mandate_id]['status'] == 'rejected'


class TestSweepExpired:
    def test_an_approved_mandate_nobody_reads_is_expired_and_cancelled(self, store):
        m = _approved(store)
        with patch('integrations.ap2.ap2_mandate.time.time',
                   return_value=m.expires_at + 1):
            assert store.sweep_expired() == 1
        assert store.get(m.mandate_id).status == 'expired'
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_nothing_due_expires_nothing(self, store):
        _approved(store)
        assert store.sweep_expired() == 0

    def test_a_new_checkout_sweeps_the_old_ones(self, store):
        old = _approved(store)
        with patch('integrations.ap2.ap2_mandate.time.time',
                   return_value=old.expires_at + 1):
            store.create_cart_mandate('u2', 'mcgroce', CART)
        assert store.get(old.mandate_id).status == 'expired'


class TestLedgerGateForMandatePayments:
    """The ledger is the one gate every caller (CLI, tool, LLM) goes through,
    so a mandate's payment is enforced THERE, not only in one tool."""

    def _mandate(self, store):
        return store.create_cart_mandate('u1', 'mcgroce', CART)

    def test_another_person_cannot_authorize_a_mandate_payment(self, store):
        m = self._mandate(store)
        assert store.ledger.authorize_payment(m.payment_id, 'user:someone-else') is False
        assert store.ledger.authorize_payment(m.payment_id, 'user:cli-operator') is False
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.APPROVAL_REQUIRED

    def test_the_owner_still_authorizes_through_the_store(self, store):
        m = self._mandate(store)
        assert store.approve(m.mandate_id, 'u1') == (True, 'approved')
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.AUTHORIZED

    def test_the_owner_may_authorize_directly_as_themselves(self, store):
        m = self._mandate(store)
        assert store.ledger.authorize_payment(m.payment_id, 'user:u1') is True

    def test_a_non_mandate_payment_is_authorized_by_any_person_as_before(self, store):
        p = store.ledger.create_payment_request(
            amount=5, currency='INR', description='plain',
            requester_agent_id='agent', require_approval=False)
        assert store.ledger.authorize_payment(p.payment_id, 'user:anyone') is True

    def test_a_mandate_payment_cannot_be_processed_directly(self, store):
        m = self._mandate(store)
        store.ledger.authorize_payment(m.payment_id, 'user:u1')
        out = store.ledger.process_payment(m.payment_id)           # the CLI's call
        assert out['success'] is False and 'only by the person' in out['error']
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.AUTHORIZED

    def test_the_settler_still_processes_it(self, store):
        m = self._mandate(store)
        store.approve(m.mandate_id, 'u1')
        out = store.ledger.process_payment(m.payment_id, settler=True)
        assert 'only by the person' not in out.get('error', '')
        assert store.ledger.get_payment(m.payment_id).status != PaymentStatus.AUTHORIZED

    def test_a_non_mandate_payment_still_processes_directly(self, store):
        p = store.ledger.create_payment_request(
            amount=5, currency='INR', description='plain',
            requester_agent_id='agent', require_approval=False)
        store.ledger.authorize_payment(p.payment_id, 'user:anyone')
        assert 'only by the person' not in store.ledger.process_payment(p.payment_id).get('error', '')


class TestGatewayUrlsTrustOnlyKnownHosts:
    """A forged Host must never become the URL the gateway sends the buyer
    and the signed confirmation to."""

    def _urls(self, host, env):
        from flask import Flask
        from integrations.ap2 import ap2_mandate
        app = Flask('t')
        with patch.dict('os.environ', env, clear=False), \
                app.test_request_context('/', headers={'Host': host}):
            import os
            if 'HARTOS_PUBLIC_URL' not in env:
                os.environ.pop('HARTOS_PUBLIC_URL', None)
            return ap2_mandate._own_gateway_urls()

    def test_a_forged_host_under_the_wildcard_is_not_used(self):
        assert self._urls('evil.example', {'ALLOWED_HOSTS': '*'}) == {}

    def test_an_explicitly_allowed_host_is_used(self):
        out = self._urls('shop.hertzai.com', {'ALLOWED_HOSTS': 'shop.hertzai.com'})
        assert out['callback_url'] == 'http://shop.hertzai.com/api/v1/intelligence/phonepe/callback'

    def test_loopback_is_used(self):
        assert self._urls('localhost:6777', {'ALLOWED_HOSTS': '*'})['redirect_url'] == 'http://localhost:6777/'

    def test_the_configured_public_url_wins_over_any_host(self):
        out = self._urls('evil.example', {'ALLOWED_HOSTS': '*',
                                          'HARTOS_PUBLIC_URL': 'https://node.example/'})
        assert out['redirect_url'] == 'https://node.example/'

    def test_outside_a_request_without_a_public_url_it_is_empty(self):
        from integrations.ap2 import ap2_mandate
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('HARTOS_PUBLIC_URL', None)
            assert ap2_mandate._own_gateway_urls() == {}


class TestMandatesDoNotStickForever:
    def _approved_with(self, store, status):
        m = _approved(store)
        store.ledger.get_payment(m.payment_id).status = status
        return m

    def _at(self, store, m, when):
        with patch('integrations.ap2.ap2_mandate.time.time', return_value=when):
            return store.get(m.mandate_id)

    def test_an_approved_mandate_whose_payment_completed_is_consumed(self, store):
        m = self._approved_with(store, PaymentStatus.COMPLETED)
        assert self._at(store, m, m.expires_at + 1).status == 'consumed'

    def test_a_processing_payment_holds_its_mandate_within_the_grace(self, store):
        from integrations.ap2.ap2_mandate import PROCESSING_RECONCILE_AFTER_S
        m = self._approved_with(store, PaymentStatus.PROCESSING)
        later = m.expires_at + PROCESSING_RECONCILE_AFTER_S - 60
        assert self._at(store, m, later).status == 'approved'

    def test_a_processing_payment_past_the_grace_releases_the_mandate_only(self, store):
        from integrations.ap2.ap2_mandate import PROCESSING_RECONCILE_AFTER_S
        m = self._approved_with(store, PaymentStatus.PROCESSING)
        later = m.expires_at + PROCESSING_RECONCILE_AFTER_S + 60
        assert self._at(store, m, later).status == 'expired'
        # money may have moved: the payment is left for a person
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.PROCESSING

    def test_an_unpaid_mandate_still_expires_and_cancels(self, store):
        m = _approved(store)
        assert self._at(store, m, m.expires_at + 1).status == 'expired'
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED
