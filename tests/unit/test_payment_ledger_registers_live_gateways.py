"""A node with a live payment key actually registers that gateway at boot.

Review of b6502ac00 (measured): PaymentLedger.__init__ built the Stripe and
PhonePe connectors and read their ``connected`` flag BEFORE anything had
called ``connect()`` (add_gateway is what connects), so with STRIPE_API_KEY,
PHONEPE_MERCHANT_ID and PHONEPE_SALT_KEY all set the registered gateways
were ['mock'].  Every paid tier therefore settled through the mock, and once
the mock was refused for customers, every paying customer on central was
told to "set STRIPE_API_KEY" while it was set.  The ledger now connects
first and registers on success.

The stripe SDK is not installed in the test venv, so the Stripe case stubs
the connector's connect(); the PhonePe case runs the real connect (it is
offline: credentials present means connected).
"""
import pytest

from integrations.ap2 import ap2_protocol as ap2
from integrations.ap2.ap2_protocol import PaymentGateway, PaymentLedger


@pytest.fixture(autouse=True)
def _no_live_keys(monkeypatch):
    for v in ('STRIPE_API_KEY', 'PHONEPE_MERCHANT_ID', 'PHONEPE_SALT_KEY',
              'PHONEPE_SALT_INDEX', 'PHONEPE_ENV'):
        monkeypatch.delenv(v, raising=False)


def _ledger(tmp_path):
    return PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))


def test_without_keys_only_the_mock_is_registered(tmp_path):
    ledger = _ledger(tmp_path)
    assert set(ledger.gateways) == {PaymentGateway.MOCK}
    assert ledger.select_gateway('USD') is PaymentGateway.MOCK
    assert ledger.select_gateway('INR') is PaymentGateway.MOCK


def test_phonepe_credentials_register_phonepe_at_boot(tmp_path, monkeypatch):
    monkeypatch.setenv('PHONEPE_MERCHANT_ID', 'M123')
    monkeypatch.setenv('PHONEPE_SALT_KEY', 'salt-abc')
    ledger = _ledger(tmp_path)
    assert PaymentGateway.PHONEPE in ledger.gateways
    assert ledger.gateways[PaymentGateway.PHONEPE].connected is True
    assert ledger.select_gateway('INR') is PaymentGateway.PHONEPE
    assert ledger.select_gateway('USD') is PaymentGateway.MOCK     # PhonePe is the INR rail


def test_half_phonepe_credentials_register_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('PHONEPE_MERCHANT_ID', 'M123')              # no salt key
    ledger = _ledger(tmp_path)
    assert PaymentGateway.PHONEPE not in ledger.gateways


def test_stripe_key_registers_stripe_when_its_connect_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv('STRIPE_API_KEY', 'sk_test_x')
    seen = []

    def connect(self):
        seen.append(self.api_key)
        self.connected = bool(self.api_key)
        return self.connected
    monkeypatch.setattr(ap2.StripePaymentGateway, 'connect', connect)
    ledger = _ledger(tmp_path)
    assert PaymentGateway.STRIPE in ledger.gateways
    assert seen and all(k == 'sk_test_x' for k in seen)
    assert ledger.select_gateway('USD') is PaymentGateway.STRIPE


def test_a_stripe_connect_that_fails_registers_nothing(tmp_path, monkeypatch):
    """No SDK or a bad key: the node stays on what it had, as the boot log says."""
    monkeypatch.setenv('STRIPE_API_KEY', 'sk_test_x')

    def connect(self):
        self.connected = False
        return False
    monkeypatch.setattr(ap2.StripePaymentGateway, 'connect', connect)
    ledger = _ledger(tmp_path)
    assert PaymentGateway.STRIPE not in ledger.gateways
    assert ledger.select_gateway('USD') is PaymentGateway.MOCK
