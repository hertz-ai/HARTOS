"""J380-J389 · Age gates / minor consent.

KidsLearning is a rich feature set; minor-specific consent flows
(COPPA / GDPR-K / India DPDP Act) are absent.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ380KidsMode:
    def test_kids_mode_disables_adult_tools(self):
        pytest.skip('J380 RED — kids mode must restrict tool set; '
                    'journey not asserted')


class TestJ381ParentConsent:
    def test_parent_consent_required_for_under_13(self):
        skip_if_missing('integrations.social.consent_service:ConsentService')
        pytest.skip('J381 RED — COPPA-style parent consent journey gap')


class TestJ382ParentDashboard:
    def test_parent_can_view_child_usage(self):
        pytest.skip('J382 RED — parent dashboard → child usage view '
                    'journey not built')


class TestJ383SchoolDistrict:
    def test_school_admin_can_enroll_class(self):
        pytest.skip('J383 RED — B2B school district enrollment journey '
                    'gap (TeacherLanding exists but flow not tested)')


class TestJ384AgeTransition:
    def test_minor_to_adult_unlocks_features(self):
        pytest.skip('J384 RED — age-threshold crossing feature unlock '
                    'journey gap')


class TestJ385MinorDataRetention:
    def test_stricter_retention_for_minors(self):
        pytest.skip('J385 RED — minor-specific retention policy gap')


class TestJ386KidsUploadModeration:
    def test_kids_post_auto_reviewed(self):
        pytest.skip('J386 RED — any content from under-13 auto enters '
                    'review queue journey untested')


class TestJ387KidsChatFilter:
    def test_stricter_constitutional_gate_in_kids_mode(self):
        skip_if_missing('security.hive_guardrails:ConstitutionalFilter')
        pytest.skip('J387 RED — kids-mode stricter gate not separately '
                    'tested from adult gate')


class TestJ388FerpaCompliance:
    def test_edu_tenant_data_isolation(self):
        pytest.skip('J388 RED — FERPA data isolation journey gap '
                    '(US education compliance)')


class TestJ389DPDPAct:
    def test_indian_minor_consent_under_dpdp(self):
        pytest.skip('J389 RED — India DPDP Act minor-consent journey gap')
