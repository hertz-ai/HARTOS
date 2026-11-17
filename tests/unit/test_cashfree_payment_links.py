"""Central's first live payment rail: Cashfree payment links.

An agent makes a link a named payer opens to pay (create_payment_link), the
payment is recorded in the AP2 ledger with the owner's billing fields, and
Cashfree's signed webhook (or the agent's check_payment_link) completes it
once, after asking Cashfree's API; the canonical revenue sum then counts it.

Everything below runs the real functions: the real PaymentLedger on a tmp
file, the real tools, the real Flask blueprint (and once the real security
middleware).  Only the HTTP boundary to Cashfree (core.http_pool) and the
owner's credential card (hartos.ai_key_vault.request_credential) are
replaced.  Signatures are made the way Cashfree documents them
(base64(HMAC-SHA256(x-webhook-timestamp + raw body, secret key))) with a
test secret.
"""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import core.config_cache as config_cache
from integrations.ap2 import ap2_protocol as ap2
from integrations.ap2.ap2_protocol import (
    MONEY_TAKEN, MockPaymentGateway, PaymentGateway, PaymentLedger, PaymentRequest,
    PaymentStatus,
)

#: The owner's billing model: every payment record carries these.
OWNER_FIELDS = ('payer_name', 'payer_phone', 'payer_email', 'child', 'plan', 'period',
                'next_due_date', 'transaction_id', 'client_id')
APP_ID = 'TEST_APP_ID_1'
SECRET = 'test-secret-key-not-real'
WEBHOOK = '/api/v1/intelligence/cashfree/webhook'
SANDBOX = 'https://sandbox.cashfree.com/pg'
LIVE = 'https://api.cashfree.com/pg'


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeCashfree:
    """Cashfree's API as the docs describe it: POST /links answers the
    LinkEntity, GET /links/{id} the link's state, GET /links/{id}/orders its
    PAID orders.  Records every call, headers included."""

    def __init__(self):
        self.posts, self.gets = [], []
        self.link_state = {}          # link_id -> dict merged into the link
        self.orders = {}              # link_id -> list of orders
        self.post_answer = None       # (status, body) to answer POST with
        self.get_raises = None
        self.gate = None              # a Barrier the GET waits on

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({'url': url, 'json': json, 'headers': dict(headers or {})})
        if self.post_answer is not None:
            return _Resp(*self.post_answer)
        return _Resp(200, {'cf_link_id': '1996567', 'link_id': json['link_id'],
                           'link_status': 'ACTIVE',
                           'link_url': f"https://payments-test.cashfree.com/links/{json['link_id']}",
                           'link_expiry_time': '2026-12-17T10:00:00+05:30',
                           'link_amount': json['link_amount'],
                           'link_currency': json['link_currency']})

    def get(self, url, headers=None, timeout=None):
        self.gets.append({'url': url, 'headers': dict(headers or {})})
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.get_raises is not None:
            raise self.get_raises
        if url.endswith('/orders'):
            link_id = url.rsplit('/', 2)[-2]
            return _Resp(200, self.orders.get(link_id, []))
        link_id = url.rsplit('/', 1)[-1]
        link = {'link_id': link_id, 'link_status': 'ACTIVE', 'link_currency': 'INR',
                'link_amount_paid': 0}
        link.update(self.link_state.get(link_id, {}))
        return _Resp(200, link)

    def links_fetched(self):
        return [g for g in self.gets if not g['url'].endswith('/orders')]


@pytest.fixture
def cf(tmp_path, monkeypatch):
    """A node with Cashfree keys, an https public address, a fresh ledger
    and the fake Cashfree API."""
    for name in ('CASHFREE_APP_ID', 'CASHFREE_SECRET_KEY', 'CASHFREE_ENV',
                 'PHONEPE_USD_INR_RATE', 'STRIPE_API_KEY', 'PHONEPE_MERCHANT_ID',
                 'PHONEPE_SALT_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_cache, '_config', {})   # no config.json / vault fallback
    monkeypatch.setenv('CASHFREE_APP_ID', APP_ID)
    monkeypatch.setenv('CASHFREE_SECRET_KEY', SECRET)
    monkeypatch.setenv('HARTOS_PUBLIC_URL', 'https://pay.example.com')
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    fake = FakeCashfree()
    monkeypatch.setattr(ap2, 'payment_ledger', ledger)
    monkeypatch.setattr('core.http_pool.pooled_post', fake.post)
    monkeypatch.setattr('core.http_pool.pooled_get', fake.get)
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    return {'ledger': ledger, 'fake': fake, 'client': app.test_client(),
            'monkeypatch': monkeypatch}


def _tools(user_id='owner-1'):
    return {t['name']: t['function']
            for t in ap2.get_ap2_tools_for_autogen('sales_agent', user_id=user_id)}


def _make_link(**overrides):
    args = dict(payer_name='Sunrise Speech Centre', payer_phone='98765 43210',
                amount_inr=4999, purpose='Speech therapy pilot, December',
                plan='speech therapy pilot', period='2026-12',
                next_due_date='2027-01-01', child_name='Asha', client_id='C-17',
                payer_email='')
    args.update(overrides)
    return json.loads(_tools()['create_payment_link'](**args))


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), ts.encode() + body,
                                     hashlib.sha256).digest()).decode()


def _event(link_id, txn=1021206):
    return json.dumps({'data': {'link_id': link_id, 'link_status': 'PAID',
                                'link_amount': '4999.00', 'link_amount_paid': '4999.00',
                                'order': {'order_id': 'CFPay_x', 'transaction_id': txn,
                                          'transaction_status': 'SUCCESS'}},
                       'type': 'PAYMENT_LINK_EVENT', 'version': 1,
                       'event_time': '2026-11-17T12:55:06+05:30'}).encode()


def _deliver(client, body, ts=None, secret=SECRET, content_type='application/json'):
    ts = ts or str(int(time.time() * 1000))
    return client.post(WEBHOOK, data=body, content_type=content_type,
                       headers={'x-webhook-timestamp': ts,
                                'x-webhook-signature': _sign(body, ts, secret)})


def _paid(fake, link_id, amount='4999.00', currency='INR'):
    fake.link_state[link_id] = {'link_status': 'PAID', 'link_amount_paid': amount,
                                'link_currency': currency}
    fake.orders[link_id] = [{'order_status': 'PAID', 'cf_order_id': '99887766',
                             'order_id': 'CFPay_x'}]


def _no_db():
    db = MagicMock()
    db.query.side_effect = RuntimeError('no social db in this test')
    return db


# ── (a) an agent makes a payable link and the record carries the owner's fields

class TestCreatePaymentLink:
    def test_a_link_is_made_on_the_sandbox_and_recorded(self, cf):
        out = _make_link()
        assert out['success'] is True, out
        fake, ledger = cf['fake'], cf['ledger']
        assert len(fake.posts) == 1
        call = fake.posts[0]
        pid = out['payment_id']
        assert call['url'] == SANDBOX + '/links'
        assert call['headers']['x-api-version'] == '2026-01-01'
        assert call['headers']['x-client-id'] == APP_ID
        assert call['headers']['x-client-secret'] == SECRET
        assert call['headers']['x-idempotency-key'] == pid
        body = call['json']
        assert body['link_id'] == f'hartos_{pid}'
        assert body['link_amount'] == 4999.0 and body['link_currency'] == 'INR'
        assert body['link_purpose'] == 'Speech therapy pilot, December'
        assert body['customer_details'] == {'customer_phone': '9876543210',
                                            'customer_name': 'Sunrise Speech Centre'}
        assert body['link_partial_payments'] is False
        assert body['link_notify'] == {'send_sms': False, 'send_email': False}
        assert body['link_meta'] == {
            'notify_url': 'https://pay.example.com/api/v1/intelligence/cashfree/webhook'}
        # Nothing about the payer or the child goes into Cashfree's notes.
        assert body['link_notes'] == {'hartos_payment_id': pid, 'client_id': 'C-17',
                                      'plan': 'speech therapy pilot', 'period': '2026-12'}
        assert out['link_url'] == f'https://payments-test.cashfree.com/links/hartos_{pid}'
        assert out['amount'] == '4999.00' and out['currency'] == 'INR'

        p = ledger.get_payment(pid)
        assert p.gateway == PaymentGateway.CASHFREE
        assert p.status == PaymentStatus.PROCESSING
        assert p.gateway_transaction_id == f'hartos_{pid}'
        assert p.amount == Decimal('4999') and p.currency == 'INR'
        assert set(OWNER_FIELDS) <= set(p.metadata)
        assert set(OWNER_FIELDS) == set(ap2.PAYMENT_LINK_RECORD_FIELDS)
        assert {k: p.metadata[k] for k in OWNER_FIELDS} == {
            'payer_name': 'Sunrise Speech Centre', 'payer_phone': '9876543210',
            'payer_email': None, 'child': 'Asha', 'plan': 'speech therapy pilot',
            'period': '2026-12', 'next_due_date': '2027-01-01',
            'transaction_id': None, 'client_id': 'C-17'}
        assert p.metadata['kind'] == 'payment_link' and p.metadata['user_id'] == 'owner-1'
        # The record survives a restart (the ledger file).
        again = PaymentLedger(ledger_path=ledger.ledger_path)
        assert again.get_payment(pid).metadata['client_id'] == 'C-17'

    def test_production_mode_uses_the_live_api(self, cf):
        cf['monkeypatch'].setenv('CASHFREE_ENV', 'production')
        assert _make_link()['success'] is True
        assert cf['fake'].posts[0]['url'] == LIVE + '/links'

    def test_without_an_https_address_no_notify_url_is_sent(self, cf):
        cf['monkeypatch'].setenv('HARTOS_PUBLIC_URL', 'http://pay.example.com')
        assert _make_link()['success'] is True
        assert 'link_meta' not in cf['fake'].posts[0]['json']

    @pytest.mark.parametrize('bad', [
        {'amount_inr': 0}, {'amount_inr': -5}, {'amount_inr': 10.005},
        {'amount_inr': 'lots'}, {'payer_phone': '12345'}, {'payer_name': ' '},
        {'purpose': ''}, {'next_due_date': '1 Jan'},
    ])
    def test_bad_input_makes_nothing(self, cf, bad):
        out = _make_link(**bad)
        assert out['success'] is False and 'Nothing was made' in out['error']
        assert cf['fake'].posts == [] and cf['ledger'].payments == {}

    def test_a_refused_link_is_recorded_failed(self, cf):
        cf['fake'].post_answer = (400, {'message': 'link_id already exists',
                                        'code': 'link_post_failed',
                                        'type': 'invalid_request_error'})
        out = _make_link()
        assert out['success'] is False and 'HTTP 400' in out['error']
        assert cf['ledger'].get_payment(out['payment_id']).status == PaymentStatus.FAILED


# ── a missing key asks the owner and never settles through the mock

class TestNoKeys:
    @pytest.mark.parametrize('had_keys_at_boot', [False, True])
    def test_missing_keys_ask_the_owner_and_never_fall_back_to_the_mock(
            self, cf, tmp_path, had_keys_at_boot):
        """A node that never had the keys (most of them), and one whose keys
        were removed after boot: neither makes a link, and the mock that is
        registered on both is never used."""
        mp = cf['monkeypatch']
        mp.delenv('CASHFREE_APP_ID')
        mp.delenv('CASHFREE_SECRET_KEY')
        ledger = cf['ledger']
        if not had_keys_at_boot:
            ledger = PaymentLedger(ledger_path=str(tmp_path / 'never.json'))
            mp.setattr(ap2, 'payment_ledger', ledger)
            assert PaymentGateway.CASHFREE not in ledger.gateways
        assert PaymentGateway.MOCK in ledger.gateways        # the mock is right there
        # A mock that "succeeds" with everything a real link answers.
        mock_ok = {'success': True, 'transaction_id': 'mock_txn_1', 'status': 'pending',
                   'redirect_url': 'https://mock.invalid/pay'}
        with patch('hartos.ai_key_vault.request_credential',
                   return_value='asked') as ask, \
                patch.object(MockPaymentGateway, 'create_payment',
                             return_value=mock_ok) as mock_pay:
            out = _make_link()
        assert out['success'] is False and 'link_url' not in out
        assert 'nobody was charged' in out['error']
        assert 'CASHFREE' not in out['error']          # the person's text names no setting
        mock_pay.assert_not_called()
        assert cf['fake'].posts == [] and ledger.payments == {}
        asked = [json.loads(c.args[0])['key_name'] for c in ask.call_args_list]
        assert asked == ['CASHFREE_APP_ID', 'CASHFREE_SECRET_KEY']
        assert all(c.kwargs.get('tell_alias') is False for c in ask.call_args_list)

    def test_only_the_missing_key_is_asked_for(self, cf):
        cf['monkeypatch'].delenv('CASHFREE_SECRET_KEY')
        with patch('hartos.ai_key_vault.request_credential', return_value='asked') as ask:
            assert _make_link()['success'] is False
        assert [json.loads(c.args[0])['key_name'] for c in ask.call_args_list] == [
            'CASHFREE_SECRET_KEY']

    def test_a_key_entered_after_boot_works_without_a_restart(self, cf, tmp_path):
        mp = cf['monkeypatch']
        mp.delenv('CASHFREE_APP_ID')
        mp.delenv('CASHFREE_SECRET_KEY')
        ledger = PaymentLedger(ledger_path=str(tmp_path / 'boot.json'))
        mp.setattr(ap2, 'payment_ledger', ledger)
        assert PaymentGateway.CASHFREE not in ledger.gateways
        mp.setenv('CASHFREE_APP_ID', APP_ID)            # what the vault delivers on entry
        mp.setenv('CASHFREE_SECRET_KEY', SECRET)
        out = _make_link()
        assert out['success'] is True
        assert PaymentGateway.CASHFREE in ledger.gateways

    def test_keys_at_boot_register_cashfree_but_never_route_carts_to_it(self, cf):
        ledger = cf['ledger']
        assert PaymentGateway.CASHFREE in ledger.gateways
        assert ledger.select_gateway('INR') is PaymentGateway.MOCK


# ── (b) the webhook: verified, idempotent, never trusts the body about money

class TestWebhook:
    def _link(self, cf):
        out = _make_link()
        return out['payment_id'], f"hartos_{out['payment_id']}"

    def test_a_signed_paid_event_records_the_payment(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        r = _deliver(cf['client'], _event(link))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['status'] == 'completed'
        p = cf['ledger'].get_payment(pid)
        assert p.status == PaymentStatus.COMPLETED
        assert p.metadata['transaction_id'] == '1021206'
        assert p.metadata['cashfree_order_id'] == '99887766'
        # Cashfree was asked about the link, with the keys in headers only.
        fetched = cf['fake'].links_fetched()
        assert fetched[0]['url'] == f'{SANDBOX}/links/{link}'
        assert fetched[0]['headers']['x-client-secret'] == SECRET

    def test_a_wrong_signature_is_refused_and_records_nothing(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        r = _deliver(cf['client'], _event(link), secret='another-secret')
        assert r.status_code == 401
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING
        assert cf['fake'].gets == []

    def test_a_body_changed_after_signing_is_refused(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        ts = str(int(time.time() * 1000))
        sig = _sign(_event(link), ts)
        r = cf['client'].post(WEBHOOK, data=_event(link, txn=999), content_type='application/json',
                              headers={'x-webhook-timestamp': ts, 'x-webhook-signature': sig})
        assert r.status_code == 401
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING

    def test_a_stale_event_is_refused(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        stale = str(int((time.time() - 6 * 60) * 1000))
        r = _deliver(cf['client'], _event(link), ts=stale)
        assert r.status_code == 401
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING

    def test_a_replayed_event_is_recorded_once(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        body = _event(link)
        ts = str(int(time.time() * 1000))
        headers = {'x-webhook-timestamp': ts, 'x-webhook-signature': _sign(body, ts)}
        first = cf['client'].post(WEBHOOK, data=body, content_type='application/json',
                                  headers=headers)
        done_at = cf['ledger'].get_payment(pid).completed_at
        second = cf['client'].post(WEBHOOK, data=body, content_type='application/json',
                                   headers=headers)
        assert first.get_json()['status'] == 'completed'
        assert second.status_code == 200
        assert second.get_json()['status'] == 'already_completed'
        assert cf['ledger'].get_payment(pid).completed_at == done_at
        assert len(cf['fake'].links_fetched()) == 1      # the replay asks nobody
        assert cf['ledger'].revenue(30) == {'INR': Decimal('4999')}

    def test_two_deliveries_at_once_complete_it_once(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link)
        cf['fake'].gate = threading.Barrier(2)
        results = []

        def run():
            results.append(ap2.settle_cashfree_link(link, cf['ledger'])['status'])
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert sorted(results) == ['already_completed', 'completed']

    def test_the_body_alone_never_completes_a_payment(self, cf):
        pid, link = self._link(cf)          # Cashfree's API still says ACTIVE
        r = _deliver(cf['client'], _event(link))
        assert r.status_code == 200 and r.get_json()['status'] == 'pending'
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING

    def test_another_amount_is_held_for_a_person_and_never_counted(self, cf):
        pid, link = self._link(cf)
        _paid(cf['fake'], link, amount='100.00')
        r = _deliver(cf['client'], _event(link))
        assert r.status_code == 200 and r.get_json()['status'] == 'amount_mismatch'
        p = cf['ledger'].get_payment(pid)
        assert p.status == PaymentStatus.FAILED and p.metadata[MONEY_TAKEN] is True
        assert cf['ledger'].revenue(30) == {}

    def test_cashfree_unreachable_answers_503_so_it_retries(self, cf):
        pid, link = self._link(cf)
        cf['fake'].get_raises = ConnectionError('down')
        r = _deliver(cf['client'], _event(link))
        assert r.status_code == 503
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING

    def test_no_keys_answers_503_and_trusts_nothing(self, cf):
        pid, link = self._link(cf)
        cf['monkeypatch'].delenv('CASHFREE_SECRET_KEY')
        r = _deliver(cf['client'], _event(link))
        assert r.status_code == 503
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.PROCESSING

    def test_a_link_this_node_did_not_make_is_acknowledged(self, cf):
        r = _deliver(cf['client'], _event('someone_elses_link'))
        assert r.status_code == 200 and r.get_json()['status'] == 'not_ours'

    def test_an_expired_link_is_marked_expired(self, cf):
        pid, link = self._link(cf)
        cf['fake'].link_state[link] = {'link_status': 'EXPIRED'}
        r = _deliver(cf['client'], _event(link))
        assert r.get_json()['status'] == 'expired'
        assert cf['ledger'].get_payment(pid).status == PaymentStatus.EXPIRED

    def test_a_signed_event_naming_no_link_is_ignored(self, cf):
        body = json.dumps({'data': {'order': {'order_id': 'o1'}},
                           'type': 'PAYMENT_SUCCESS_WEBHOOK'}).encode()
        r = _deliver(cf['client'], body)
        assert r.status_code == 200 and r.get_json()['status'] == 'ignored'


@pytest.fixture
def central(cf, monkeypatch):
    """The blueprint behind security.middleware as hart_intelligence_entry
    applies it on a central node with no API key."""
    for name, value in (('ALLOWED_HOSTS', 'localhost'), ('HEVOLVE_ENV', 'production'),
                        ('HEVOLVE_NODE_TIER', 'central'), ('HEVOLVE_API_KEY', ''),
                        ('CORS_ORIGINS', '')):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    from integrations.agent_engine.commercial_api import commercial_api_bp
    from security.middleware import apply_security_middleware
    app = Flask(__name__)
    apply_security_middleware(app)
    app.register_blueprint(commercial_api_bp)
    return dict(cf, client=app.test_client())


class TestWebhookOnCentral:
    def test_a_json_event_reaches_the_route(self, central):
        out = _make_link()
        link = f"hartos_{out['payment_id']}"
        _paid(central['fake'], link)
        r = _deliver(central['client'], _event(link))
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()['status'] == 'completed'

    def test_a_form_event_passes_the_csrf_guard_and_completes(self, central):
        """Cashfree's payment-link page says link events come as form data."""
        out = _make_link()
        link = f"hartos_{out['payment_id']}"
        _paid(central['fake'], link)
        body = f'link_id={link}&link_status=PAID'.encode()
        r = _deliver(central['client'], body,
                     content_type='application/x-www-form-urlencoded')
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()['status'] == 'completed'
        assert central['ledger'].get_payment(out['payment_id']).metadata[
            'transaction_id'] == '99887766'

    def test_an_unsigned_form_post_is_still_refused(self, central):
        out = _make_link()
        r = central['client'].post(WEBHOOK, data={'link_id': f"hartos_{out['payment_id']}"})
        assert r.status_code == 401
        assert central['ledger'].get_payment(out['payment_id']).status == \
            PaymentStatus.PROCESSING


# ── the agent's own check records a payment when no webhook arrives

class TestCheckPaymentLink:
    def test_a_paid_link_is_recorded_by_the_check(self, cf):
        out = _make_link()
        _paid(cf['fake'], f"hartos_{out['payment_id']}")
        got = json.loads(_tools()['check_payment_link'](out['payment_id']))
        assert got['paid'] is True and got['status'] == 'completed'
        assert got['transaction_id'] == '99887766'

    def test_an_unpaid_link_says_so(self, cf):
        out = _make_link()
        got = json.loads(_tools()['check_payment_link'](out['payment_id']))
        assert got['paid'] is False and got['status'] == 'processing'

    def test_another_persons_link_is_not_shown(self, cf):
        out = _make_link()
        got = json.loads(_tools(user_id='someone-else')['check_payment_link'](
            out['payment_id']))
        assert got['success'] is False


# ── the payer's details and the child's name stay in the ledger file

PRIVATE = {'payer_name': 'Zinnia Quillfeather Centre', 'payer_phone': '9123456780',
           'payer_email': 'zinnia.payer@example.org', 'child_name': 'Kavya Testchild'}


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        self.lines.append(text)


@pytest.fixture
def log_lines():
    """Every record, from the root and from the loggers the paths use
    (attached to each, so a logger that does not propagate is read too)."""
    handler = _Collect()
    names = ('', 'agent_logger', 'hevolve_social', 'tool_execution',
             'integrations.ap2.ap2_protocol', 'hevolve_core', 'hevolve_security')
    saved = []
    for name in names:
        lg = logging.getLogger(name)
        saved.append((lg, lg.level))
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler.lines
    for lg, level in saved:
        lg.removeHandler(handler)
        lg.setLevel(level)


def _leaks(texts):
    return [t for t in texts for v in PRIVATE.values() if v in t]


class TestPayerDetailsNeverLeaveTheLedger:
    def test_a_link_its_webhook_and_a_check_log_none_of_them(self, cf, log_lines):
        from core.tool_logging import log_tool_execution
        tools = {t['name']: log_tool_execution(t['function'])     # as create/reuse wrap them
                 for t in ap2.get_ap2_tools_for_autogen('sales_agent', user_id='owner-1')}
        made = tools['create_payment_link'](
            amount_inr=1500, purpose='Spoken English batch, January', plan='english batch',
            period='2027-01', next_due_date='2027-02-01', client_id='C-9', **PRIVATE)
        pid = json.loads(made)['payment_id']
        link = f'hartos_{pid}'
        _paid(cf['fake'], link, amount='1500.00')
        body = json.dumps({'data': {'link_id': link, 'link_status': 'PAID',
                                    'customer_details': {
                                        'customer_name': PRIVATE['payer_name'],
                                        'customer_phone': PRIVATE['payer_phone'],
                                        'customer_email': PRIVATE['payer_email']},
                                    'order': {'transaction_id': 5551}},
                           'type': 'PAYMENT_LINK_EVENT'}).encode()
        hook = _deliver(cf['client'], body)
        checked = tools['check_payment_link'](pid)
        assert hook.get_json()['status'] == 'completed'
        assert json.loads(checked)['paid'] is True
        assert any('TOOL EXECUTION START: create_payment_link' in t for t in log_lines)
        assert any("'payer_name': '<withheld>'" in t for t in log_lines)
        assert _leaks(log_lines) == []
        assert _leaks([made, checked, hook.get_data(as_text=True)]) == []
        # ...and the ledger file is where they are.
        p = cf['ledger'].get_payment(pid)
        assert p.metadata['payer_phone'] == PRIVATE['payer_phone']
        assert p.metadata['child'] == PRIVATE['child_name']

    def test_a_refusal_that_echoes_the_phone_is_not_repeated(self, cf, log_lines):
        cf['fake'].post_answer = (400, {
            'message': f"customer_phone {PRIVATE['payer_phone']} is invalid",
            'code': 'customer_details.customer_phone_invalid',
            'type': 'invalid_request_error'})
        out = _tools()['create_payment_link'](
            amount_inr=10, purpose='x', **PRIVATE)
        assert json.loads(out)['success'] is False
        assert 'customer_details.customer_phone_invalid' in out
        assert _leaks([out]) == [] and _leaks(log_lines) == []


# ── (c) the canonical revenue sum counts it

def _completed(ledger, kind, gateway, amount, currency):
    p = ledger.create_payment_request(
        amount=Decimal(amount), currency=currency, description='x',
        requester_agent_id='a', gateway=gateway, metadata={'kind': kind})
    p.update_status(PaymentStatus.COMPLETED)
    return p


class TestRevenue:
    def test_a_paid_link_is_in_total_gross(self, cf):
        out = _make_link()
        _paid(cf['fake'], f"hartos_{out['payment_id']}")
        _deliver(cf['client'], _event(f"hartos_{out['payment_id']}"))
        from integrations.agent_engine.revenue_aggregator import query_revenue_streams
        streams = query_revenue_streams(_no_db(), period_days=30)
        assert streams['payment_revenue'] == {'INR': 4999.0}
        assert streams['payment_revenue_usd'] == pytest.approx(4999 / 84.0)
        assert streams['total_gross'] == pytest.approx(4999 / 84.0)
        assert streams['user_pool_share'] == pytest.approx(4999 / 84.0 * 0.90)

    def test_only_live_money_in_is_counted(self, cf):
        ledger = cf['ledger']
        _completed(ledger, 'payment_link', PaymentGateway.CASHFREE, '1000', 'INR')
        _completed(ledger, 'tier_upgrade', PaymentGateway.STRIPE, '9', 'USD')     # task #210
        _completed(ledger, 'tier_upgrade', PaymentGateway.MOCK, '49', 'USD')      # no money moved
        _completed(ledger, 'commerce_checkout', PaymentGateway.PHONEPE, '500', 'INR')
        ledger.create_payment_request(                                            # not paid yet
            amount=Decimal('700'), currency='INR', description='x', requester_agent_id='a',
            gateway=PaymentGateway.CASHFREE, metadata={'kind': 'payment_link'})
        old = _completed(ledger, 'payment_link', PaymentGateway.CASHFREE, '300', 'INR')
        old.completed_at = old.completed_at.replace(year=old.completed_at.year - 1)
        assert ledger.revenue(30) == {'INR': Decimal('1000'), 'USD': Decimal('9')}
        cf['monkeypatch'].setenv('PHONEPE_USD_INR_RATE', '100')
        from integrations.agent_engine.revenue_aggregator import query_revenue_streams
        streams = query_revenue_streams(_no_db(), period_days=30)
        assert streams['payment_revenue_usd'] == pytest.approx(9 + 10)
        assert streams['total_gross'] == pytest.approx(19)

    def test_phonepe_prices_with_the_same_rate(self, cf):
        cf['monkeypatch'].setenv('PHONEPE_USD_INR_RATE', '90')
        gw = ap2.PhonePePaymentGateway(merchant_id='M', salt_key='S')
        assert gw.usd_inr_rate == ap2.operator_usd_inr_rate() == 90.0
        req = PaymentRequest(Decimal('9'), 'USD', 'x', 'a')
        assert gw._to_paise(req) == 81000
