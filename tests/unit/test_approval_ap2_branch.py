"""The commerce branches of POST /api/agent/approval, driven through the REAL
hart_intelligence_entry app (imported once per module; ~15 s).

ap2_pay:<payment_id>          -> mandate approve / reject as the JWT user
merchant_onboard:<draft_id>   -> McGroce admin create, as the JWT user
merchant_sku:<draft_id>       -> same, for a product

The approver must come from the verified Bearer JWT; a body user_id is
ignored, a different user gets 403, no token gets 401.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from integrations.ap2.ap2_mandate import MandateStore
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus
from integrations.commerce.drafts import DraftStore
from integrations.social.auth import generate_jwt

CART = {'lines': [{'sku_id': 11, 'qty': 2, 'unit_price': 40}],
        'total': 80, 'currency': 'INR'}
OWNER, OTHER = 'mcg-4242', 'mcg-5'


@pytest.fixture(scope='module')
def app():
    """The real entry app.  Importing it reconfigures process-wide logging
    (root level INFO, a handler, its RequestLogRecord factory); that is put
    back afterwards, because later tests that mock time.time with a fixed
    side_effect list see every extra INFO record consume a tick (measured:
    test_bind_game_sound's still-running case failed with StopIteration
    only when run after this module)."""
    import logging
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), logging.getLogRecordFactory())
    os.environ.setdefault('HEVOLVE_START_BACKGROUND_SERVICES', '0')
    hie = pytest.importorskip('hart_intelligence_entry')
    hie.app.config['TESTING'] = True
    yield hie.app
    root.setLevel(saved[0])
    for h in list(root.handlers):
        if h not in saved[1]:
            root.removeHandler(h)
    logging.setLogRecordFactory(saved[2])


@pytest.fixture
def ctx(app, tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    mandates = MandateStore(str(tmp_path / 'm.json'), ledger=ledger, key=b'k' * 32)
    drafts = DraftStore(str(tmp_path / 'd.json'))
    mcg = MagicMock()
    mcg.onboard_merchant.return_value = {'success': True, 'status': 201,
                                         'data': {'id': 31}}
    mcg.create_product.return_value = {'success': True, 'status': 201,
                                       'data': {'id': 99}}
    mcg.base_url, mcg.admin_url, mcg.admin_key = 'https://m.example/api/v1', '', ''
    mcg.breaker.get_stats.return_value = {'state': 'closed'}
    with patch('integrations.ap2.ap2_mandate.get_mandate_store', return_value=mandates), \
            patch('integrations.commerce.drafts.get_draft_store', return_value=drafts), \
            patch('integrations.commerce.mcgroce_client.get_client', return_value=mcg), \
            patch('integrations.agent_engine.liquid_ui_service.push_agent_ui',
                  return_value=True) as push:
        yield {'client': app.test_client(), 'mandates': mandates,
               'ledger': ledger, 'drafts': drafts, 'mcg': mcg, 'push': push}


def _answer(ctx, action, decision='approve', user=OWNER, body_user=None):
    headers = {}
    if user:
        headers['Authorization'] = 'Bearer ' + generate_jwt(user, user, tenant_id='mcgroce')
    body = {'agent_id': 'mcgroce', 'action': action, 'decision': decision}
    if body_user:
        body['user_id'] = body_user
    return ctx['client'].post('/api/agent/approval', json=body, headers=headers)


class TestAp2Pay:
    def test_owner_approval_authorizes_as_the_jwt_user_and_settles(self, ctx):
        m = ctx['mandates'].create_cart_mandate(OWNER, 'mcgroce', CART)
        r = _answer(ctx, f'ap2_pay:{m.payment_id}')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'approved'
        p = ctx['ledger'].get_payment(m.payment_id)
        assert p.status == PaymentStatus.COMPLETED
        assert p.approval_chain[-1]['approver_id'] == f'user:{OWNER}'
        assert ctx['mandates'].get(m.mandate_id).status == 'consumed'
        shown = [c.args[1]['type'] for c in ctx['push'].call_args_list]
        assert shown[-1] == 'payment_status'

    def test_another_user_gets_403_even_naming_the_owner_in_the_body(self, ctx):
        m = ctx['mandates'].create_cart_mandate(OWNER, 'mcgroce', CART)
        r = _answer(ctx, f'ap2_pay:{m.payment_id}', user=OTHER, body_user=OWNER)
        assert r.status_code == 403
        assert ctx['ledger'].get_payment(m.payment_id).status == PaymentStatus.APPROVAL_REQUIRED
        assert ctx['mandates'].get(m.mandate_id).status == 'pending'

    def test_no_token_is_401(self, ctx):
        m = ctx['mandates'].create_cart_mandate(OWNER, 'mcgroce', CART)
        r = _answer(ctx, f'ap2_pay:{m.payment_id}', user=None, body_user=OWNER)
        assert r.status_code == 401
        assert ctx['mandates'].get(m.mandate_id).status == 'pending'

    def test_decline_rejects_and_cancels(self, ctx):
        m = ctx['mandates'].create_cart_mandate(OWNER, 'mcgroce', CART)
        r = _answer(ctx, f'ap2_pay:{m.payment_id}', decision='deny')
        assert r.status_code == 200 and r.get_json()['status'] == 'denied'
        assert ctx['ledger'].get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_second_approval_conflicts(self, ctx):
        m = ctx['mandates'].create_cart_mandate(OWNER, 'mcgroce', CART)
        assert _answer(ctx, f'ap2_pay:{m.payment_id}').status_code == 200
        assert _answer(ctx, f'ap2_pay:{m.payment_id}').status_code == 409

    def test_unknown_payment_is_404(self, ctx):
        assert _answer(ctx, 'ap2_pay:nope').status_code == 404


class TestMerchantDrafts:
    def test_owner_approval_submits_the_merchant_once(self, ctx):
        d = ctx['drafts'].create(OWNER, 'merchant', {'displayName': 'Asha Fresh'})
        r = _answer(ctx, f"merchant_onboard:{d['draft_id']}")
        assert r.status_code == 200 and r.get_json()['applied'] is True
        ctx['mcg'].onboard_merchant.assert_called_once_with({'displayName': 'Asha Fresh'})
        assert ctx['drafts'].get(d['draft_id'])['status'] == 'submitted'
        # a double tap does not create it twice
        assert _answer(ctx, f"merchant_onboard:{d['draft_id']}").status_code == 409
        assert ctx['mcg'].onboard_merchant.call_count == 1

    def test_other_user_cannot_submit(self, ctx):
        d = ctx['drafts'].create(OWNER, 'merchant', {'displayName': 'X'})
        assert _answer(ctx, f"merchant_onboard:{d['draft_id']}", user=OTHER).status_code == 403
        ctx['mcg'].onboard_merchant.assert_not_called()

    def test_sku_draft_kind_must_match_the_action(self, ctx):
        d = ctx['drafts'].create(OWNER, 'sku', {'name': 'Ghee'})
        assert _answer(ctx, f"merchant_onboard:{d['draft_id']}").status_code == 404
        r = _answer(ctx, f"merchant_sku:{d['draft_id']}")
        assert r.status_code == 200
        ctx['mcg'].create_product.assert_called_once_with({'name': 'Ghee'})

    def test_mcgroce_refusal_is_502_and_recorded(self, ctx):
        ctx['mcg'].onboard_merchant.return_value = {'success': False, 'error': 'dup'}
        d = ctx['drafts'].create(OWNER, 'merchant', {'displayName': 'X'})
        assert _answer(ctx, f"merchant_onboard:{d['draft_id']}").status_code == 502
        assert ctx['drafts'].get(d['draft_id'])['status'] == 'failed'


def test_non_commerce_actions_still_take_the_old_path(ctx):
    r = _answer(ctx, 'enable_camera', decision='deny')
    assert r.status_code == 200
    assert r.get_json() == {'status': 'denied', 'action': 'enable_camera'}


def test_commerce_blueprint_is_registered_on_the_entry_app(ctx):
    r = ctx['client'].get('/api/commerce/health')
    assert r.status_code == 200
    assert r.get_json()['configured'] is True


def test_blueprint_registry_mounts_commerce_for_the_nunba_path():
    from integrations.blueprint_registry import register_all_blueprints
    from flask import Flask
    app = Flask(__name__)
    result = register_all_blueprints(app)
    assert 'commerce' in result['registered']
    assert '/api/commerce/session' in {r.rule for r in app.url_map.iter_rules()}
    # a second pass (the standalone entry already mounted it) is a no-op
    again = register_all_blueprints(app)
    assert 'commerce' in again['skipped']


@pytest.mark.parametrize('action', ['ap2_pay:pay_123', 'merchant_onboard:d1',
                                    'MERCHANT_SKU:d2'])
def test_a_commerce_answer_is_refused_when_commerce_cannot_import(app, action):
    """Fail closed: with integrations.commerce.approvals unimportable, a
    commerce answer must not fall through to the consent record (which would
    write a consent row for the owner and answer as if applied)."""
    from tests.unit.module_swap import swap_modules
    with swap_modules({'integrations.commerce.approvals': None}), \
            patch('integrations.social.consent_service.ConsentService.'
                  'record_capability_decision') as record:
        resp = app.test_client().post('/api/agent/approval', json={
            'agent_id': 'a', 'action': action, 'decision': 'approve'})
    assert resp.status_code == 503
    assert 'nothing was approved or charged' in resp.get_json()['reason']
    record.assert_not_called()
