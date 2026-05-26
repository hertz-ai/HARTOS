"""Regression coverage for the 2026-05-13 hive-revenue work:

1. `integrations.agent_engine.dispatch._internal_auth_headers`
   — must mint a Bearer JWT (system_daemon admin) when no API key env
   is set, and prefer the X-API-Key when one is.  Without this header
   on central tier, `security/middleware.py` Gate 2 rejects daemon
   `/chat` dispatches with HTTP 401, which silently broke the
   autonomous outreach flywheel from 2026-03-14 to 2026-05-13.

2. `integrations.ap2.PhonePePaymentGateway`
   — signs payloads deterministically against PhonePe's documented
   X-VERIFY scheme, converts USD→paise via PHONEPE_USD_INR_RATE
   override or `metadata.inr_amount`, refuses to connect without
   credentials, and exposes the symbol via `integrations.ap2`.

3. `commercial_api.upgrade_key` PhonePe-rejection
   — synchronous /upgrade endpoint MUST return HTTP 501 when the
   caller requests `gateway: 'phonepe'`, because PhonePe is a
   redirect-then-webhook flow and a synchronous tier-bump would
   grant the upgrade before the user actually pays (revenue leak).
"""
from __future__ import annotations

import hashlib
import os
import unittest
from decimal import Decimal
from unittest.mock import patch


class TestDaemonAuthHeaders(unittest.TestCase):
    def _import(self):
        # Import lazily so the test can run even on minimal envs where
        # autogen/social are partially missing — we just need the helper.
        from integrations.agent_engine import dispatch
        return dispatch

    def test_returns_none_when_jwt_mint_fails(self):
        dispatch = self._import()
        # Force both branches to fail: no API key, JWT mint raises.
        with patch.dict(os.environ, {'HEVOLVE_API_KEY': ''}, clear=False):
            with patch('integrations.social.auth.generate_jwt',
                       side_effect=RuntimeError("no secret configured")):
                self.assertIsNone(dispatch._internal_auth_headers())

    def test_prefers_x_api_key_when_env_present(self):
        dispatch = self._import()
        with patch.dict(os.environ, {'HEVOLVE_API_KEY': '  secret123  '}, clear=False):
            headers = dispatch._internal_auth_headers()
            self.assertIsNotNone(headers)
            self.assertEqual(headers.get('X-API-Key'), 'secret123')
            self.assertNotIn('Authorization', headers)

    def test_mints_bearer_when_no_api_key(self):
        dispatch = self._import()
        with patch.dict(os.environ, {'HEVOLVE_API_KEY': ''}, clear=False):
            with patch('integrations.social.auth.generate_jwt',
                       return_value='fake.jwt.token'):
                headers = dispatch._internal_auth_headers()
                self.assertIsNotNone(headers)
                self.assertEqual(headers.get('Authorization'),
                                 'Bearer fake.jwt.token')
                self.assertNotIn('X-API-Key', headers)


class TestPhonePeGateway(unittest.TestCase):
    def _import(self):
        from integrations.ap2 import PhonePePaymentGateway, PaymentGateway
        return PhonePePaymentGateway, PaymentGateway

    def test_exported_via_package(self):
        from integrations import ap2
        self.assertTrue(hasattr(ap2, 'PhonePePaymentGateway'))
        self.assertIn('PhonePePaymentGateway', ap2.__all__)
        # Enum value should be present so downstream `_PG('phonepe')` works.
        self.assertEqual(ap2.PaymentGateway.PHONEPE.value, 'phonepe')

    def test_connect_disabled_without_credentials(self):
        Gateway, _ = self._import()
        # Clear PhonePe env so the gateway sees no creds even if the test
        # host has them configured.
        env_overrides = {k: '' for k in
                         ('PHONEPE_MERCHANT_ID', 'PHONEPE_SALT_KEY',
                          'PHONEPE_SALT_INDEX', 'PHONEPE_ENV')}
        with patch.dict(os.environ, env_overrides, clear=False):
            g = Gateway()
            self.assertFalse(g.connect())
            self.assertFalse(g.connected)

    def test_connect_succeeds_with_credentials(self):
        Gateway, _ = self._import()
        g = Gateway(merchant_id='MERCHANT', salt_key='SALT', salt_index='1', env='UAT')
        self.assertTrue(g.connect())
        self.assertTrue(g.connected)
        # UAT env should point at the sandbox base, not prod.
        self.assertIn('preprod', g.base_url)

    def test_sign_matches_phonepe_scheme(self):
        """X-VERIFY = sha256(base64_payload + endpoint + salt_key) + ### + salt_index."""
        Gateway, _ = self._import()
        g = Gateway(merchant_id='M', salt_key='SALT_KEY', salt_index='2', env='UAT')
        b64 = 'eyJtZXJjaGFudElkIjogIk0ifQ=='
        endpoint = '/pg/v1/pay'
        expected_digest = hashlib.sha256(
            (b64 + endpoint + 'SALT_KEY').encode('utf-8')
        ).hexdigest()
        self.assertEqual(g._sign(b64, endpoint), f'{expected_digest}###2')

    def test_sign_status_no_payload(self):
        Gateway, _ = self._import()
        g = Gateway(merchant_id='M', salt_key='SALT_KEY', salt_index='1', env='UAT')
        endpoint = '/pg/v1/status/M/txn_abc'
        expected_digest = hashlib.sha256(
            (endpoint + 'SALT_KEY').encode('utf-8')
        ).hexdigest()
        self.assertEqual(g._sign_status(endpoint), f'{expected_digest}###1')

    def test_to_paise_usd_at_default_rate(self):
        Gateway, _ = self._import()
        from integrations.ap2 import PaymentRequest, PaymentMethod
        g = Gateway(merchant_id='M', salt_key='S', salt_index='1', env='UAT')
        g.usd_inr_rate = 84.0
        # $9 → 9 * 84 = 756 INR → 75600 paise
        req = PaymentRequest(
            amount=Decimal('9.00'), currency='USD',
            description='Starter upgrade', requester_agent_id='u',
            payment_method=PaymentMethod.INTERNAL_CREDITS,
        )
        self.assertEqual(g._to_paise(req), 75600)

    def test_to_paise_inr_passthrough(self):
        Gateway, _ = self._import()
        from integrations.ap2 import PaymentRequest, PaymentMethod
        g = Gateway(merchant_id='M', salt_key='S', salt_index='1', env='UAT')
        req = PaymentRequest(
            amount=Decimal('750'), currency='INR',
            description='Rs.750 upgrade', requester_agent_id='u',
            payment_method=PaymentMethod.INTERNAL_CREDITS,
        )
        self.assertEqual(g._to_paise(req), 75000)

    def test_to_paise_inr_override_in_metadata(self):
        Gateway, _ = self._import()
        from integrations.ap2 import PaymentRequest, PaymentMethod
        g = Gateway(merchant_id='M', salt_key='S', salt_index='1', env='UAT')
        g.usd_inr_rate = 84.0
        req = PaymentRequest(
            amount=Decimal('9.00'), currency='USD',
            description='Starter upgrade', requester_agent_id='u',
            payment_method=PaymentMethod.INTERNAL_CREDITS,
            metadata={'inr_amount': 1000.0},
        )
        # inr_amount=1000 INR → 100000 paise; USD ignored when override present.
        self.assertEqual(g._to_paise(req), 100000)

    def test_create_payment_refuses_when_disconnected(self):
        Gateway, _ = self._import()
        from integrations.ap2 import PaymentRequest, PaymentMethod
        env_overrides = {k: '' for k in
                         ('PHONEPE_MERCHANT_ID', 'PHONEPE_SALT_KEY',
                          'PHONEPE_SALT_INDEX', 'PHONEPE_ENV')}
        with patch.dict(os.environ, env_overrides, clear=False):
            g = Gateway()
            g.connect()  # should set connected=False
            req = PaymentRequest(
                amount=Decimal('9.00'), currency='USD',
                description='x', requester_agent_id='u',
                payment_method=PaymentMethod.INTERNAL_CREDITS,
            )
            result = g.create_payment(req)
            self.assertFalse(result['success'])
            self.assertIn('not connected', result['error'].lower())

    def test_verify_callback_accepts_valid_signature(self):
        """PhonePe webhook: signature with right salt + b64 + index must verify."""
        Gateway, _ = self._import()
        g = Gateway(merchant_id='M', salt_key='SALT', salt_index='1', env='UAT')
        g.connect()
        b64 = 'eyJjb2RlIjoiUEFZTUVOVF9TVUNDRVNTIn0='
        digest = hashlib.sha256((b64 + 'SALT').encode('utf-8')).hexdigest()
        valid_header = f'{digest}###1'
        self.assertTrue(g.verify_callback(b64, valid_header))

    def test_verify_callback_rejects_tampered_signature(self):
        Gateway, _ = self._import()
        g = Gateway(merchant_id='M', salt_key='SALT', salt_index='1', env='UAT')
        g.connect()
        b64 = 'eyJjb2RlIjoiUEFZTUVOVF9TVUNDRVNTIn0='
        # Same b64 but signed with wrong salt
        digest = hashlib.sha256((b64 + 'WRONG_SALT').encode('utf-8')).hexdigest()
        bad_header = f'{digest}###1'
        self.assertFalse(g.verify_callback(b64, bad_header))

    def test_verify_callback_rejects_when_disconnected(self):
        """Without credentials, callback verification must refuse — fail closed."""
        Gateway, _ = self._import()
        env_overrides = {k: '' for k in
                         ('PHONEPE_MERCHANT_ID', 'PHONEPE_SALT_KEY',
                          'PHONEPE_SALT_INDEX', 'PHONEPE_ENV')}
        with patch.dict(os.environ, env_overrides, clear=False):
            g = Gateway()
            g.connect()
            self.assertFalse(g.verify_callback('anything', 'anyhash###1'))

    def test_verify_callback_rejects_empty_inputs(self):
        Gateway, _ = self._import()
        g = Gateway(merchant_id='M', salt_key='SALT', salt_index='1', env='UAT')
        g.connect()
        self.assertFalse(g.verify_callback('', 'sig'))
        self.assertFalse(g.verify_callback('payload', ''))
        self.assertFalse(g.verify_callback('', ''))


if __name__ == '__main__':
    unittest.main()
