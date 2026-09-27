"""AP2 mandates: an agent can ask for money, only its owner can say yes.

Behavioural: every test drives the real PaymentLedger / tool functions /
decide_payment, with the UI push and the node secret as the only boundaries.
"""
import json
from decimal import Decimal

import pytest

from integrations.ap2 import ap2_mandate, ap2_protocol
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    led = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    # The agent tools read the module-level ledger; point it at this one.
    monkeypatch.setattr(ap2_protocol, 'payment_ledger', led)
    return led


@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    pushes = []
    monkeypatch.setattr(ap2_mandate, '_push',
                        lambda comp, uid: pushes.append((comp, uid)) or True)
    monkeypatch.setattr(ap2_mandate, '_node_key', lambda: b'k' * 32)
    return pushes


def _tools(user_id='42'):
    tools = ap2_protocol.get_ap2_tools_for_autogen('assistant', user_id=user_id)
    return {t['name']: t['function'] for t in tools}


def _agent_requests(user_id='42', amount=120.5):
    tools = _tools(user_id)
    out = json.loads(tools['request_payment'](amount, 'INR', 'Groceries'))
    return tools, out['payment_id']


def test_the_agent_cannot_authorize_its_own_payment(ledger, boundaries):
    tools, pid = _agent_requests()
    result = json.loads(tools['authorize_payment'](pid, approver_id='system'))

    assert result['status'] == 'approval_required'
    assert ledger.get_payment(pid).status == PaymentStatus.APPROVAL_REQUIRED
    processed = json.loads(tools['process_payment'](pid))
    assert processed['success'] is False
    # The owner was shown the card, with the action the route decodes.
    comp, uid = boundaries[-1]
    assert uid == '42'
    assert comp['type'] == 'approval'
    assert ap2_mandate.parse_approval_action(comp['action']) == pid


def test_a_bare_ledger_authorize_cannot_bypass_the_mandate(ledger):
    _, pid = _agent_requests()
    # Even with the owner's own id, no signed approval -> refused.
    assert ledger.authorize_payment(pid, '42') is False
    assert ledger.get_payment(pid).status == PaymentStatus.PENDING


def test_the_owner_approves_and_the_payment_completes(ledger, boundaries):
    _, pid = _agent_requests()
    payload, status = ap2_mandate.decide_payment(pid, '42', True, ledger=ledger)

    assert status == 200 and payload['status'] == 'approved'
    payment = ledger.get_payment(pid)
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.approval_chain[0]['approver_id'] == '42'
    assert boundaries[-1][0]['type'] == 'payment_status'
    assert boundaries[-1][0]['status'] == 'completed'


def test_someone_else_cannot_approve(ledger):
    _, pid = _agent_requests()
    payload, status = ap2_mandate.decide_payment(pid, '99', True, ledger=ledger)
    assert status == 403
    assert ledger.get_payment(pid).status == PaymentStatus.PENDING


def test_no_identity_is_refused(ledger):
    _, pid = _agent_requests()
    _, status = ap2_mandate.decide_payment(pid, None, True, ledger=ledger)
    assert status == 401


def test_a_payment_without_an_owner_cannot_be_approved(ledger):
    _, pid = _agent_requests(user_id=None)
    _, status = ap2_mandate.decide_payment(pid, '42', True, ledger=ledger)
    assert status == 403


def test_deny_cancels_and_tells_the_hook(ledger):
    seen = []
    ap2_mandate.register_payment_hook('generic', lambda p, o: seen.append(o))
    try:
        _, pid = _agent_requests()
        payload, status = ap2_mandate.decide_payment(pid, '42', False,
                                                     ledger=ledger)
    finally:
        ap2_mandate._hooks['generic'].clear()
    assert status == 200 and payload['status'] == 'denied'
    assert ledger.get_payment(pid).status == PaymentStatus.CANCELLED
    assert seen == ['denied']


def test_a_tampered_amount_voids_the_approval(ledger):
    _, pid = _agent_requests()
    payment = ledger.get_payment(pid)
    ledger.set_metadata(pid, ap2_mandate.APPROVAL_KEY,
                        ap2_mandate._approval_record(payment, '42'))
    payment.amount = Decimal('9999')          # amount changed after approval
    assert ledger.authorize_payment(pid, '42') is False


def test_an_approval_cannot_be_replayed_onto_another_payment(ledger):
    _, pid_a = _agent_requests()
    _, pid_b = _agent_requests()
    record = ap2_mandate._approval_record(ledger.get_payment(pid_a), '42')
    ledger.set_metadata(pid_b, ap2_mandate.APPROVAL_KEY, record)
    assert ledger.authorize_payment(pid_b, '42') is False


def test_non_human_approvers_are_refused_on_plain_payments(ledger):
    payment = ledger.create_payment_request(
        amount=Decimal('5'), currency='INR', description='x',
        requester_agent_id='assistant')
    assert ledger.authorize_payment(payment.payment_id, 'system') is False
    assert ledger.authorize_payment(payment.payment_id, 'assistant') is False
    assert ledger.authorize_payment(payment.payment_id, 'user:7') is True


def test_the_ledger_accessor_hart_cli_imports_exists():
    assert ap2_protocol.get_payment_ledger() is ap2_protocol.payment_ledger
