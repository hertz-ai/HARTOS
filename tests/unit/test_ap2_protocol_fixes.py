"""Behavioural tests for the ap2_protocol fixes the McGroce commerce work needs.

Covers: live-gateway selection (request_payment no longer hard-codes MOCK),
the ledger's default path under core.platform_paths, gateway I/O running
OUTSIDE the ledger lock, the redirect-gateway (PhonePe) path staying
PROCESSING instead of being failed by an immediate capture, the approval-
required start state, cancel, get_payment_ledger (hart pay), and the LLM no
longer being offered authorize_payment by default.
"""
import json
import os
import threading
from decimal import Decimal
from unittest.mock import patch

import pytest

from integrations.ap2 import ap2_protocol as ap2
from integrations.ap2.ap2_protocol import (
    PaymentGateway, PaymentGatewayConnector, PaymentLedger, PaymentStatus,
)


class _Gateway(PaymentGatewayConnector):
    """A gateway double whose create_payment can inspect the ledger lock."""

    def __init__(self, gw=PaymentGateway.MOCK, create_result=None,
                 capture_result=None, ledger_ref=None):
        super().__init__(gw)
        self.create_result = create_result or {'success': True,
                                               'transaction_id': 'txn_1'}
        self.capture_result = capture_result or {'success': True,
                                                 'status': 'captured'}
        self.ledger_ref = ledger_ref
        self.lock_free_during_create = None
        self.capture_calls = 0

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        if self.ledger_ref is not None:
            got = self.ledger_ref[0].lock.acquire(blocking=False)
            self.lock_free_during_create = got
            if got:
                self.ledger_ref[0].lock.release()
        return dict(self.create_result)

    def capture_payment(self, payment_id, gateway_transaction_id):
        self.capture_calls += 1
        return dict(self.capture_result)


def _ledger(tmp_path):
    return PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))


def _authorized(ledger, gateway=PaymentGateway.MOCK, currency='INR'):
    p = ledger.create_payment_request(
        amount=Decimal('120.00'), currency=currency, description='t',
        requester_agent_id='agent:shop', gateway=gateway)
    assert ledger.authorize_payment(p.payment_id, 'user:1')
    return p.payment_id


class TestSelectGateway:
    def test_mock_only_node_falls_back_to_mock(self, tmp_path):
        ledger = _ledger(tmp_path)
        ledger.gateways = {PaymentGateway.MOCK: ledger.gateways[PaymentGateway.MOCK]}
        assert ledger.select_gateway('INR') == PaymentGateway.MOCK
        assert ledger.select_gateway('USD') == PaymentGateway.MOCK

    def test_inr_prefers_phonepe_then_stripe(self, tmp_path):
        ledger = _ledger(tmp_path)
        ledger.gateways[PaymentGateway.STRIPE] = _Gateway(PaymentGateway.STRIPE)
        assert ledger.select_gateway('inr') == PaymentGateway.STRIPE
        ledger.gateways[PaymentGateway.PHONEPE] = _Gateway(PaymentGateway.PHONEPE)
        assert ledger.select_gateway('INR') == PaymentGateway.PHONEPE

    def test_usd_never_goes_to_phonepe(self, tmp_path):
        ledger = _ledger(tmp_path)
        ledger.gateways[PaymentGateway.PHONEPE] = _Gateway(PaymentGateway.PHONEPE)
        assert ledger.select_gateway('USD') == PaymentGateway.MOCK

    def test_request_payment_tool_uses_the_selected_gateway(self, tmp_path):
        ledger = _ledger(tmp_path)
        ledger.gateways[PaymentGateway.STRIPE] = _Gateway(PaymentGateway.STRIPE)
        with patch.object(ap2, 'payment_ledger', ledger):
            fn = ap2.create_payment_request_function('agent')
            out = json.loads(fn(amount=10, currency='USD', description='x'))
        assert ledger.get_payment(out['payment_id']).gateway == PaymentGateway.STRIPE


class TestLedgerPath:
    def test_default_path_is_under_agent_data_dir(self, tmp_path):
        with patch('core.platform_paths.get_agent_data_dir',
                   return_value=str(tmp_path / 'agent_data')):
            ledger = PaymentLedger()
        assert ledger.ledger_path == os.path.join(
            str(tmp_path / 'agent_data'), 'payment_ledger.json')

    def test_old_cwd_ledger_is_copied_once_never_deleted(self, tmp_path, monkeypatch):
        old = PaymentLedger(ledger_path=str(tmp_path / 'cwd' / 'agent_data'
                                            / 'payment_ledger.json'))
        p = old.create_payment_request(
            amount=Decimal('9'), currency='INR', description='before the move',
            requester_agent_id='user:1')
        monkeypatch.chdir(tmp_path / 'cwd')
        new_dir = tmp_path / 'data'
        with patch('core.platform_paths.get_agent_data_dir',
                   return_value=str(new_dir)):
            first = PaymentLedger()
            assert first.get_payment(p.payment_id) is not None
            assert os.path.exists(old.ledger_path)          # never deleted
            # a later write to the new file is not overwritten by the old one
            first.cancel_payment(p.payment_id, 'user:1')
            again = PaymentLedger()
        assert again.get_payment(p.payment_id).status == PaymentStatus.CANCELLED


class TestProcessPaymentLocking:
    def test_gateway_call_runs_without_the_ledger_lock(self, tmp_path):
        ledger = _ledger(tmp_path)
        ref = [ledger]
        gw = _Gateway(ledger_ref=ref)
        ledger.gateways[PaymentGateway.MOCK] = gw
        pid = _authorized(ledger)
        result = ledger.process_payment(pid)
        assert result['success'] is True
        assert gw.lock_free_during_create is True
        assert ledger.get_payment(pid).status == PaymentStatus.COMPLETED

    def test_concurrent_second_call_is_refused_while_processing(self, tmp_path):
        ledger = _ledger(tmp_path)
        entered, release = threading.Event(), threading.Event()

        class _Slow(_Gateway):
            def create_payment(self, payment_request):
                entered.set()
                release.wait(5)
                return {'success': True, 'transaction_id': 'txn_slow'}

        gw = _Slow()
        ledger.gateways[PaymentGateway.MOCK] = gw
        pid = _authorized(ledger)
        results = {}
        t = threading.Thread(target=lambda: results.setdefault(
            'first', ledger.process_payment(pid)))
        t.start()
        assert entered.wait(5)
        second = ledger.process_payment(pid)
        release.set()
        t.join(5)
        assert second['success'] is False
        assert 'not authorized' in second['error']
        assert results['first']['success'] is True
        assert gw.capture_calls == 1

    def test_gateway_exception_marks_failed(self, tmp_path):
        ledger = _ledger(tmp_path)

        class _Boom(_Gateway):
            def create_payment(self, payment_request):
                raise RuntimeError('network down')

        ledger.gateways[PaymentGateway.MOCK] = _Boom()
        pid = _authorized(ledger)
        result = ledger.process_payment(pid)
        assert result == {'success': False, 'error': 'network down'}
        assert ledger.get_payment(pid).status == PaymentStatus.FAILED


class TestRedirectGateway:
    def test_phonepe_stays_processing_and_is_not_captured(self, tmp_path):
        ledger = _ledger(tmp_path)
        gw = _Gateway(PaymentGateway.PHONEPE, create_result={
            'success': True, 'transaction_id': 'hartos_abc',
            'redirect_url': 'https://pay.example/checkout'})
        ledger.gateways[PaymentGateway.PHONEPE] = gw
        pid = _authorized(ledger, gateway=PaymentGateway.PHONEPE)
        result = ledger.process_payment(pid)
        assert result['success'] is False
        assert result['status'] == 'redirect_required'
        assert result['redirect_url'] == 'https://pay.example/checkout'
        assert gw.capture_calls == 0
        p = ledger.get_payment(pid)
        assert p.status == PaymentStatus.PROCESSING
        assert p.gateway_transaction_id == 'hartos_abc'


class TestApprovalRequiredAndCancel:
    def test_approval_required_payment_can_be_authorized_once(self, tmp_path):
        ledger = _ledger(tmp_path)
        p = ledger.create_payment_request(
            amount=Decimal('5'), currency='INR', description='x',
            requester_agent_id='agent:shop', require_approval=True)
        assert p.status == PaymentStatus.APPROVAL_REQUIRED
        assert ledger.process_payment(p.payment_id)['success'] is False
        assert ledger.authorize_payment(p.payment_id, 'user:1') is True
        assert ledger.authorize_payment(p.payment_id, 'user:1') is False

    def test_cancel_before_processing(self, tmp_path):
        ledger = _ledger(tmp_path)
        p = ledger.create_payment_request(
            amount=Decimal('5'), currency='INR', description='x',
            requester_agent_id='agent:shop', require_approval=True)
        assert ledger.cancel_payment(p.payment_id, 'user:1', 'denied') is True
        assert ledger.get_payment(p.payment_id).status == PaymentStatus.CANCELLED
        assert ledger.authorize_payment(p.payment_id, 'user:1') is False

    def test_cancel_refused_after_completion(self, tmp_path):
        ledger = _ledger(tmp_path)
        pid = _authorized(ledger)
        assert ledger.process_payment(pid)['success'] is True
        assert ledger.cancel_payment(pid, 'user:1') is False
        assert ledger.cancel_payment('missing', 'user:1') is False


class TestToolsAndCli:
    def test_get_payment_ledger_is_the_singleton(self):
        assert ap2.get_payment_ledger() is ap2.payment_ledger
        from integrations.ap2 import get_payment_ledger
        assert get_payment_ledger() is ap2.payment_ledger

    def test_default_authorize_tool_only_asks(self, tmp_path, monkeypatch):
        """The model gets authorize_payment (saved recipes still resolve), but
        by default it can only show the owner the card -- never authorize."""
        monkeypatch.delenv('AP2_ALLOW_LLM_AUTHORIZE', raising=False)
        from integrations.ap2.ap2_mandate import MandateStore
        ledger = _ledger(tmp_path)
        store = MandateStore(str(tmp_path / 'm.json'), ledger=ledger, key=b'k' * 32)
        tools = {t['name']: t['function']
                 for t in ap2.get_ap2_tools_for_autogen('agent', user_id='u1')}
        assert list(tools) == ['request_payment', 'authorize_payment', 'process_payment']
        with patch.object(ap2, 'payment_ledger', ledger), \
                patch('integrations.ap2.ap2_mandate.get_mandate_store', return_value=store), \
                patch('integrations.agent_engine.liquid_ui_service.push_agent_ui',
                      return_value=True) as push:
            req = json.loads(tools['request_payment'](
                amount=99, currency='INR', description='tea'))
            assert req['status'] == 'approval_required'
            pid = req['payment_id']
            out = json.loads(tools['authorize_payment'](pid, approver_id='u1'))
        assert out['status'] == 'approval_required'
        assert ledger.get_payment(pid).status == PaymentStatus.APPROVAL_REQUIRED
        cards = [c.args[1] for c in push.call_args_list]
        assert cards[-1]['type'] == 'approval'
        assert cards[-1]['action'] == f'ap2_pay:{pid}'
        assert push.call_args.kwargs['user_id'] == 'u1'

    def test_ask_only_tool_refuses_an_ownerless_payment(self, tmp_path):
        from integrations.ap2.ap2_mandate import MandateStore
        ledger = _ledger(tmp_path)
        store = MandateStore(str(tmp_path / 'm.json'), ledger=ledger, key=b'k' * 32)
        p = ledger.create_payment_request(
            amount=Decimal('5'), currency='INR', description='x',
            requester_agent_id='agent')
        fn = ap2.create_payment_authorization_function(ask_only=True)
        with patch('integrations.ap2.ap2_mandate.get_mandate_store', return_value=store):
            out = json.loads(fn(p.payment_id, approver_id='anyone'))
        assert out['success'] is False
        assert ledger.get_payment(p.payment_id).status == PaymentStatus.PENDING

    def test_env_flag_gives_the_direct_tool(self, monkeypatch):
        monkeypatch.setenv('AP2_ALLOW_LLM_AUTHORIZE', '1')
        tools = ap2.get_ap2_tools_for_autogen('a')
        desc = next(t['description'] for t in tools if t['name'] == 'authorize_payment')
        assert desc.startswith('Authorize a pending payment')


class TestLedgerRefusesNonHumanApprovers:
    @pytest.mark.parametrize('approver', ['system', 'SYSTEM', '', 'assistant', 'llm'])
    def test_non_human_ids_are_refused(self, tmp_path, approver):
        ledger = _ledger(tmp_path)
        p = ledger.create_payment_request(
            amount=Decimal('5'), currency='INR', description='x',
            requester_agent_id='agent:shop')
        assert ledger.authorize_payment(p.payment_id, approver) is False
        assert ledger.get_payment(p.payment_id).status == PaymentStatus.PENDING

    def test_the_requesting_agent_cannot_approve_itself(self, tmp_path):
        ledger = _ledger(tmp_path)
        p = ledger.create_payment_request(
            amount=Decimal('5'), currency='INR', description='x',
            requester_agent_id='agent:shop')
        assert ledger.authorize_payment(p.payment_id, 'agent:shop') is False
        assert ledger.authorize_payment(p.payment_id, 'user:1') is True

    def test_hart_cli_pay_list_no_longer_reports_unavailable(self, tmp_path):
        click = pytest.importorskip('click')
        from click.testing import CliRunner
        try:
            from hartos.hart_cli import hart
        except Exception as e:  # pragma: no cover - env without CLI deps
            pytest.skip(f'hart_cli import failed: {e}')
        ledger = _ledger(tmp_path)
        ledger.create_payment_request(
            amount=Decimal('3'), currency='INR', description='cli test',
            requester_agent_id='cli_user')
        with patch.object(ap2, 'payment_ledger', ledger):
            res = CliRunner().invoke(
                hart, ['--json', '--user-id', 'cli_user', 'pay', 'list'])
        assert 'AP2 not available' not in res.output
        listed = json.loads(res.output)
        assert [p['description'] for p in listed] == ['cli test']


# ── ledger durability and credentials (review of 22df2e2c1 and save/load) ──

def _good_record(ledger, **meta):
    from decimal import Decimal as _D
    return ledger.create_payment_request(
        amount=_D('5'), currency='INR', description='x',
        requester_agent_id='agent', metadata=meta or None)


class TestLedgerDurability:
    def test_a_payment_method_token_is_never_written_or_returned(self, tmp_path):
        import json as _json
        from integrations.ap2.ap2_protocol import PaymentLedger
        ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
        p = _good_record(ledger, payment_method_token='pm_secret', kind='tier_upgrade')
        assert 'payment_method_token' not in p.to_dict()['metadata']
        assert p.to_dict()['metadata']['kind'] == 'tier_upgrade'
        assert 'pm_secret' not in (tmp_path / 'l.json').read_text()
        # still in memory for the gateway call in the same request
        assert p.metadata['payment_method_token'] == 'pm_secret'

    def test_an_unreadable_ledger_is_copied_aside_before_any_save(self, tmp_path):
        from integrations.ap2.ap2_protocol import PaymentLedger
        path = tmp_path / 'l.json'
        path.write_text('{ this is not json')
        ledger = PaymentLedger(ledger_path=str(path))
        _good_record(ledger)                          # a save happens here
        backups = list(tmp_path.glob('l.json.unreadable-*'))
        assert len(backups) == 1 and backups[0].read_text() == '{ this is not json'

    def test_one_bad_record_does_not_cost_the_others(self, tmp_path):
        import json as _json
        from integrations.ap2.ap2_protocol import PaymentLedger
        path = tmp_path / 'l.json'
        seed = PaymentLedger(ledger_path=str(path))
        good = _good_record(seed)
        data = _json.loads(path.read_text())
        data['payments']['broken'] = {'amount': 'not-a-number'}
        path.write_text(_json.dumps(data))
        ledger = PaymentLedger(ledger_path=str(path))
        assert good.payment_id in ledger.payments and 'broken' not in ledger.payments
        assert list(tmp_path.glob('l.json.unreadable-*'))

    def test_legacy_records_the_new_ledger_lacks_are_merged_once(self, tmp_path):
        import json as _json
        from integrations.ap2.ap2_protocol import PaymentLedger
        old_path = tmp_path / 'old.json'
        old = PaymentLedger(ledger_path=str(old_path))
        legacy_p = _good_record(old)
        new = PaymentLedger(ledger_path=str(tmp_path / 'new.json'))
        own = _good_record(new)
        new._legacy_path = str(old_path)
        new._merge_legacy()
        assert legacy_p.payment_id in new.payments and own.payment_id in new.payments
        assert (tmp_path / 'new.json.legacy_merged').exists()
        reread = PaymentLedger(ledger_path=str(tmp_path / 'new.json'))
        assert legacy_p.payment_id in reread.payments
        # a second merge changes nothing
        del new.payments[legacy_p.payment_id]
        new._merge_legacy()
        assert legacy_p.payment_id not in new.payments

    def test_a_saved_ledger_reloads_identically(self, tmp_path):
        from integrations.ap2.ap2_protocol import PaymentLedger
        a = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
        p = _good_record(a, kind='x')
        b = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
        assert b.get_payment(p.payment_id).to_dict() == p.to_dict()
