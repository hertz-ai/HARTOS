"""J260-J269 · Billing / payment lifecycle.

Untested billing = silent money loss.  Renew, upgrade, downgrade,
card decline, refund, invoice download.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ260SubscriptionRenew:
    def test_renewal_extends_subscription_and_awards_spark(self):
        skip_if_missing('integrations.agent_engine:commercial_api')
        pytest.skip('J260 RED — subscription renewal pipeline E2E gap')

    def test_failed_renewal_triggers_grace_period(self):
        pytest.skip('J260b RED — card-decline grace-period journey '
                    'untested')


class TestJ261Upgrade:
    def test_plan_upgrade_prorates_correctly(self):
        pytest.skip('J261 RED — plan upgrade proration journey gap')


class TestJ262Downgrade:
    def test_plan_downgrade_at_period_end(self):
        pytest.skip(
            'J262 RED — downgrade-at-period-end behavior untested; '
            'user could lose data if downgrade enforces feature limits '
            'retroactively'
        )


class TestJ263CardDecline:
    def test_card_decline_retry_chain(self):
        pytest.skip('J263 RED — card-decline retry chain + smart-retry '
                    'schedule is a classic revenue-lost bug surface')

    def test_final_decline_soft_suspends_account(self):
        pytest.skip(
            'J263b RED — "soft suspend" vs "hard delete" path needs '
            'consent + grace-period assertions; not journey-tested'
        )


class TestJ264Refund:
    def test_refund_reverses_spark_award(self):
        skip_if_missing(
            'integrations.agent_engine.revenue_aggregator:'
            'settle_metered_api_costs',
        )
        pytest.skip(
            'J264 RED — refund should reverse the ResonanceTransaction '
            'rows created by the original charge; journey gap'
        )


class TestJ265InvoiceDownload:
    def test_invoice_pdf_generated_and_signed(self):
        pytest.skip(
            'J265 RED — invoice PDF journey untested (likely needed for '
            'tax/GST compliance in India + enterprise users)'
        )


class TestJ266TaxGst:
    def test_gst_calculated_per_state(self):
        pytest.skip('J266 RED — GST per-state calculation logic untested')


class TestJ267SparkWithdraw:
    def test_first_withdraw_triggers_payment_setup_consent(self):
        skip_if_missing(
            'integrations.social.consent_service:ConsentService',
        )
        pytest.skip(
            'J267 RED — J246-equivalent journey (first withdraw → '
            'payment_setup consent dialog) E2E gap'
        )

    def test_withdraw_below_minimum_rejects(self):
        pytest.skip('J267b RED — withdraw-minimum-threshold UX untested')


class TestJ268PhonePe:
    def test_phonepe_payment_callback_settles(self):
        skip_if_missing('phonepay')
        pytest.skip('J268 RED — PhonePe callback → ledger journey gap')


class TestJ269Webhook:
    def test_payment_provider_webhook_signature_verified(self):
        pytest.skip(
            'J269 RED — payment-webhook signature verification is '
            'a security-critical path; no journey asserts rejection '
            'of unsigned / replay-attack webhooks'
        )
