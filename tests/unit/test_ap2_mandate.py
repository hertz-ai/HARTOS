"""Behavioural tests for integrations/ap2/ap2_mandate.py.

A real PaymentLedger (isolated tmp path, Mock gateway) sits behind a real
MandateStore; nothing is mocked except time where expiry is under test.
"""
import json
from unittest.mock import patch

import pytest

from integrations.ap2.ap2_mandate import (
    MandateError, MandateStore, canonical_cart_hash,
)
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus

CART = {
    'lines': [{'sku_id': 11, 'qty': 2, 'unit_price': 40},
              {'sku_id': 12, 'qty': 1, 'unit_price': {'amount': 160, 'currency': 'INR'}}],
    'total': {'amount': 240, 'currency': 'INR'},
    'currency': 'INR',
}


@pytest.fixture
def store(tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    return MandateStore(path=str(tmp_path / 'ap2_mandates.json'),
                        ledger=ledger, key=b'k' * 32)


class TestCartHash:
    def test_line_order_does_not_matter(self):
        flipped = dict(CART, lines=list(reversed(CART['lines'])))
        assert canonical_cart_hash(flipped) == canonical_cart_hash(CART)

    def test_display_fields_do_not_matter(self):
        named = dict(CART, lines=[dict(l, name='Milk', image='x.png')
                                  for l in CART['lines']])
        assert canonical_cart_hash(named) == canonical_cart_hash(CART)

    @pytest.mark.parametrize('mutate', [
        lambda c: c['lines'][0].update(qty=3),
        lambda c: c['lines'][0].update(unit_price=39),
        lambda c: c['lines'][0].update(sku_id=99),
        lambda c: c.update(total=241),
        lambda c: c.update(currency='USD'),
    ])
    def test_any_payable_change_changes_the_hash(self, mutate):
        other = json.loads(json.dumps(CART))
        mutate(other)
        assert canonical_cart_hash(other) != canonical_cart_hash(CART)

    def test_malformed_line_raises(self):
        with pytest.raises(MandateError):
            canonical_cart_hash({'lines': [{'qty': 1}], 'total': 1})


class TestCreate:
    def test_creates_pending_mandate_and_approval_required_payment(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        assert m.status == 'pending'
        assert m.amount == '240.00' and m.currency == 'INR'
        p = store.ledger.get_payment(m.payment_id)
        assert p.status == PaymentStatus.APPROVAL_REQUIRED
        assert p.metadata['mandate_id'] == m.mandate_id
        # persisted
        raw = json.load(open(store.path))
        assert raw['mandates'][m.mandate_id]['cart_hash'] == m.cart_hash

    def test_payment_metadata_carries_this_nodes_gateway_urls(
            self, store, monkeypatch):
        monkeypatch.setenv('HARTOS_PUBLIC_URL', 'https://node7.example/')
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        meta = store.ledger.get_payment(m.payment_id).metadata
        assert meta['callback_url'] == (
            'https://node7.example/api/v1/intelligence/phonepe/callback')
        assert meta['redirect_url'].startswith('https://node7.example')

    def test_no_public_base_leaves_the_gateway_default(self, store, monkeypatch):
        monkeypatch.delenv('HARTOS_PUBLIC_URL', raising=False)
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        meta = store.ledger.get_payment(m.payment_id).metadata
        assert 'callback_url' not in meta

    def test_over_cap_is_refused(self, store):
        with pytest.raises(MandateError, match='over the cap'):
            store.create_cart_mandate('u1', 'mcgroce', CART, cap=200)
        assert store.ledger.list_payments() == []

    def test_cap_is_recorded_as_intent(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART, cap=500)
        assert m.intent['cap'] == '500.00'

    @pytest.mark.parametrize('cart', [
        {'lines': [], 'total': 10, 'currency': 'INR'},
        {'lines': [{'sku_id': 1, 'qty': 1, 'unit_price': 0}], 'total': 0},
    ])
    def test_empty_or_free_cart_refused(self, store, cart):
        with pytest.raises(MandateError):
            store.create_cart_mandate('u1', 'mcgroce', cart)

    def test_mandates_survive_a_reload(self, store, tmp_path):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        again = MandateStore(path=store.path, ledger=store.ledger, key=b'k' * 32)
        assert again.get(m.mandate_id).cart_hash == m.cart_hash


class TestApprove:
    def test_owner_approves_and_payment_is_authorized(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        assert store.approve(m.mandate_id, 'u1') == (True, 'approved')
        p = store.ledger.get_payment(m.payment_id)
        assert p.status == PaymentStatus.AUTHORIZED
        assert p.approval_chain[-1]['approver_id'] == 'user:u1'
        assert store.get(m.mandate_id).approver_id == 'u1'

    def test_another_user_cannot_approve(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        ok, reason = store.approve(m.mandate_id, 'u2')
        assert not ok and 'owner' in reason
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.APPROVAL_REQUIRED

    def test_cannot_approve_twice(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        store.approve(m.mandate_id, 'u1')
        assert store.approve(m.mandate_id, 'u1') == (False, 'mandate is approved')

    def test_tampered_record_is_refused(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        store._mandates[m.mandate_id].user_id = 'attacker'
        ok, reason = store.approve(m.mandate_id, 'attacker')
        assert not ok and 'signature' in reason

    def test_reject_cancels_the_payment(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        assert store.reject(m.mandate_id, 'u1') == (True, 'rejected')
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED
        assert store.approve(m.mandate_id, 'u1')[0] is False

    def test_find_by_payment_id(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        assert store.find_by_payment_id(m.payment_id).mandate_id == m.mandate_id
        assert store.find_by_payment_id('nope') is None


class TestVerifyForCheckout:
    def _approved(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        store.approve(m.mandate_id, 'u1')
        return m

    def test_approved_matching_cart_passes(self, store):
        m = self._approved(store)
        assert store.verify_for_checkout(m.mandate_id, 'u1', CART) == (True, 'ok')

    def test_missing(self, store):
        assert store.verify_for_checkout('mdt_x', 'u1', CART) == (False, 'mandate not found')

    def test_pending_refused(self, store):
        m = store.create_cart_mandate('u1', 'mcgroce', CART)
        ok, reason = store.verify_for_checkout(m.mandate_id, 'u1', CART)
        assert not ok and 'not approved' in reason

    def test_wrong_owner_refused(self, store):
        m = self._approved(store)
        ok, reason = store.verify_for_checkout(m.mandate_id, 'u2', CART)
        assert not ok and 'another user' in reason

    def test_cart_drift_refused(self, store):
        m = self._approved(store)
        drifted = json.loads(json.dumps(CART))
        drifted['lines'][0]['qty'] = 5
        ok, reason = store.verify_for_checkout(m.mandate_id, 'u1', drifted)
        assert not ok and 'cart changed' in reason

    def test_expired_refused_and_payment_cancelled(self, store):
        m = self._approved(store)
        with patch('integrations.ap2.ap2_mandate.time.time',
                   return_value=m.expires_at + 1):
            ok, reason = store.verify_for_checkout(m.mandate_id, 'u1', CART)
        assert not ok and 'expired' in reason
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_consume_is_one_shot(self, store):
        m = self._approved(store)
        assert store.consume(m.mandate_id) is True
        assert store.consume(m.mandate_id) is False
        ok, reason = store.verify_for_checkout(m.mandate_id, 'u1', CART)
        assert not ok and 'consumed' in reason


class TestDecidePayment:
    """The ONE place a person's answer is applied (the approval routes call it)."""

    @pytest.fixture(autouse=True)
    def _no_ui(self):
        with patch('integrations.agent_engine.liquid_ui_service.push_agent_ui',
                   return_value=True) as push:
            self.push = push
            yield

    def test_owner_approval_settles_a_generic_mandate(self, store):
        from integrations.ap2.ap2_mandate import decide_payment
        m = store.create_cart_mandate('u1', 'shop', CART)
        payload, code = decide_payment(m.payment_id, 'u1', True, store=store)
        assert code == 200 and payload['result']['success'] is True
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.COMPLETED
        assert store.get(m.mandate_id).status == 'consumed'
        assert self.push.call_args.args[1]['type'] == 'payment_status'
        assert self.push.call_args.kwargs['user_id'] == 'u1'

    @pytest.mark.parametrize('who,code', [(None, 401), ('', 401), ('u2', 403)])
    def test_only_the_owner_may_answer(self, store, who, code):
        from integrations.ap2.ap2_mandate import decide_payment
        m = store.create_cart_mandate('u1', 'shop', CART)
        assert decide_payment(m.payment_id, who, True, store=store)[1] == code
        assert store.get(m.mandate_id).status == 'pending'

    def test_decline_cancels(self, store):
        from integrations.ap2.ap2_mandate import decide_payment
        m = store.create_cart_mandate('u1', 'shop', CART)
        payload, code = decide_payment(m.payment_id, 'u1', False, store=store)
        assert (code, payload['status']) == (200, 'denied')
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_unknown_payment(self, store):
        from integrations.ap2.ap2_mandate import decide_payment
        assert decide_payment('nope', 'u1', True, store=store)[1] == 404

    def test_a_kind_settles_through_its_registered_settler(self, store):
        from integrations.ap2 import ap2_mandate
        seen = []
        ap2_mandate.register_settler('test_kind', lambda m: seen.append(m.mandate_id) or {'success': True})
        try:
            m = store.create_cart_mandate('u1', 'shop', CART, kind='test_kind')
            payload, _ = ap2_mandate.decide_payment(m.payment_id, 'u1', True, store=store)
        finally:
            ap2_mandate._settlers.pop('test_kind', None)
        assert seen == [m.mandate_id] and payload['result'] == {'success': True}
        # the settler owns the money step: the ledger only authorized it
        assert store.ledger.get_payment(m.payment_id).status == PaymentStatus.AUTHORIZED

    def test_the_mandate_requester_is_the_agent_not_the_owner(self, store):
        m = store.create_cart_mandate('u1', 'shop', CART)
        assert store.ledger.get_payment(m.payment_id).requester_agent_id == 'ap2:shop'
