"""A person (``user:<id>``) authorizing the payment they requested is legal;
an agent authorizing its own request is not.  Drives the real tier-upgrade
route and the real ``hart pay authorize`` command through the real guard."""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from integrations.ap2.ap2_protocol import PaymentLedger


def _ledger(tmp_path):
    return PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))


def test_person_may_authorize_their_own_request(tmp_path):
    ledger = _ledger(tmp_path)
    p = ledger.create_payment_request(
        amount=Decimal('5'), currency='USD', description='x',
        requester_agent_id='user:7')
    assert ledger.authorize_payment(p.payment_id, 'user:7') is True


@pytest.mark.parametrize('agent_id', ['agent:shop', 'system', 'cli_user'])
def test_agent_still_cannot_authorize_itself(tmp_path, agent_id):
    ledger = _ledger(tmp_path)
    p = ledger.create_payment_request(
        amount=Decimal('5'), currency='USD', description='x',
        requester_agent_id=agent_id)
    assert ledger.authorize_payment(p.payment_id, agent_id) is False


def test_tier_upgrade_route_authorizes_through_the_real_guard(tmp_path):
    from integrations.agent_engine.commercial_api import commercial_api_bp
    ledger = _ledger(tmp_path)
    user = MagicMock(id=7)
    db = MagicMock()
    api_key = MagicMock(tier='free')
    db.query.return_value.filter_by.return_value.first.return_value = api_key
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(user, db)), \
            patch('integrations.ap2.payment_ledger', ledger):
        res = app.test_client().post(
            '/api/v1/intelligence/keys/k1/upgrade',
            json={'target_tier': 'starter', 'payment_method': 'tok_mock',
                  'gateway': 'mock'},
            headers={'Authorization': 'Bearer t'})
    assert res.status_code != 402, res.get_json()
    assert res.get_json()['success'] is True


def test_hart_pay_authorize_lets_the_cli_person_authorize(tmp_path):
    pytest.importorskip('click')
    from click.testing import CliRunner
    from integrations.ap2 import ap2_protocol as ap2
    try:
        from hartos.hart_cli import hart
    except Exception as e:  # pragma: no cover
        pytest.skip(f'hart_cli import failed: {e}')
    ledger = _ledger(tmp_path)
    p = ledger.create_payment_request(
        amount=Decimal('3'), currency='INR', description='cli',
        requester_agent_id='cli_user')
    with patch.object(ap2, 'payment_ledger', ledger):
        res = CliRunner().invoke(
            hart, ['--json', '--user-id', 'cli_user', 'pay', 'authorize',
                   p.payment_id])
    assert '"success": true' in res.output, res.output
