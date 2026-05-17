"""J250-J259 · Onboarding funnel.

Untested = silent churn.  Signup, email verify, first-boot wizard,
tier selection, tutorial completion.  See
memory/user_journey_coverage.md bucket 3.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ250Signup:
    """J250 · Guest → authenticated user via signup form."""

    def test_signup_creates_user_row(self):
        skip_if_missing('integrations.social.models:User')
        pytest.skip('J250 — signup E2E driver not wired (auth endpoints '
                    'exist; journey test absent)')

    def test_signup_idempotent_same_email(self):
        skip_if_missing('integrations.social.models:User')
        pytest.skip('J250b — duplicate-signup idempotency E2E gap')


class TestJ251EmailVerify:
    """J251 · Email verification link round-trip."""

    def test_verify_token_flips_user_state(self):
        pytest.skip(
            'J251 RED — email verification token pipeline not '
            'journey-tested; happy-path exists in unit test only'
        )

    def test_expired_token_rejected_with_clear_error(self):
        pytest.skip('J251b RED — expired-token UX sad path untested')


class TestJ252FirstBootWizard:
    """J252 · First-boot wizard steps land on HART profile."""

    def test_wizard_completes_seals_profile(self):
        skip_if_missing('hart_onboarding:get_hart_profile')
        pytest.skip(
            'J252 — wizard has unit coverage on each step; full '
            'funnel walkthrough (language → tier → hardware check → '
            'agent creation) is not a single E2E journey'
        )


class TestJ253TierSelect:
    """J253 · Node tier selection (flat/regional/central)."""

    def test_tier_sets_env_and_persists_across_reboot(self):
        skip_if_missing('security.key_delegation:get_node_tier')
        pytest.skip('J253 RED — tier-persistence across process '
                    'restart not journey-tested')


class TestJ254TutorialCompletion:
    """J254 · Tutorial progression + achievement award."""

    def test_tutorial_awards_first_achievement(self):
        skip_if_missing('integrations.social.gamification_service:'
                        'GamificationService')
        pytest.skip('J254 RED — tutorial-to-achievement E2E gap')


class TestJ255GuestToFirstChat:
    """J255 · Guest user → first chat turn → history persists."""

    def test_guest_chat_creates_anonymous_history(self):
        pytest.skip(
            'J255 — existing J01 covers chat; guest-specific path '
            '(no user_id, localStorage only) not explicitly asserted'
        )


class TestJ256HartName:
    """J256 · HART name registry idempotency + uniqueness."""

    def test_generate_then_seal_deterministic(self):
        skip_if_missing('hart_onboarding:HARTNameRegistry')
        pytest.skip('J256 — hart_name generation + seal unit-tested; '
                    'end-to-end cross-session persistence gap')


class TestJ257ConsentMatrix:
    """J257 · Onboarding consent matrix (data_access, compute,
    public_exposure, payment_setup, revenue_share)."""

    def test_onboarding_presents_only_compute_consent_first(self):
        skip_if_missing('integrations.social.consent_service:ConsentService')
        pytest.skip(
            'J257 RED — per-consent staging (compute first, payment '
            'deferred, etc.) is documented in '
            'idle_compute_workstream.md but not enforced by tests'
        )


class TestJ258ReferralLanding:
    """J258 · Signup via referral link attributes correctly."""

    def test_referral_code_awards_both_parties(self):
        skip_if_missing('integrations.social.referrals:ReferralService')
        pytest.skip('J258 RED — referral E2E attribution untested')


class TestJ259DropOffTelemetry:
    """J259 · Abandoned onboarding emits telemetry."""

    def test_partial_funnel_emits_dropoff_event(self):
        pytest.skip(
            'J259 RED — no onboarding drop-off telemetry today; '
            'retention analysis blocked until onboarding.step events '
            'are emitted on EventBus'
        )
