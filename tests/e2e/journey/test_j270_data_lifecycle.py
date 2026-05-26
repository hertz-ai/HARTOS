"""J270-J279 · Data lifecycle (GDPR-style).

Untested = regulatory liability.  Export-my-data, delete-my-account,
right-to-be-forgotten, audit log of own actions.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ270ExportMyData:
    def test_export_includes_all_transactions(self):
        skip_if_missing('integrations.social.models:ResonanceTransaction')
        pytest.skip(
            'J270 RED — GDPR-style "give me everything you have on me" '
            'endpoint (/api/me/export) not wired; journey asserts: user, '
            'posts, consents, wallet, agents, conversation history'
        )

    def test_export_excludes_other_users_data(self):
        pytest.skip('J270b RED — privacy boundary on export untested')


class TestJ271DeleteAccount:
    def test_delete_removes_pii_but_preserves_hive_learning(self):
        pytest.skip(
            'J271 RED — account-delete journey has subtle contract: '
            'PII (email, name) must be erased, but anonymized hive '
            'learning (already passed through secret_redactor) stays. '
            'No test asserts this boundary'
        )

    def test_delete_is_reversible_within_grace_period(self):
        pytest.skip('J271b RED — 30-day grace-period for delete untested')


class TestJ272RightToBeForgotten:
    def test_forget_cascades_to_federation_peers(self):
        skip_if_missing('integrations.agent_engine.federated_aggregator:'
                        'get_federated_aggregator')
        pytest.skip(
            'J272 RED — forget-me request must cascade to peers who '
            'have anonymized deltas from this user; no federation-wide '
            'forget protocol exists yet'
        )


class TestJ273AuditLogSelfView:
    def test_user_can_view_own_audit_log(self):
        skip_if_missing('security.immutable_audit_log:AuditLogEntry')
        pytest.skip('J273 RED — AuditLogEntry exists but user-scoped '
                    'self-view endpoint not built')


class TestJ274ConsentHistory:
    def test_consent_change_log_readable_by_user(self):
        skip_if_missing('integrations.social.consent_service:ConsentService')
        pytest.skip('J274 RED — consent-audit self-view gap')


class TestJ275DataPortability:
    def test_export_is_machine_readable_json(self):
        pytest.skip('J275 RED — GDPR Art.20 machine-readable export '
                    'format not journey-tested')


class TestJ276RetentionPolicy:
    def test_inactive_accounts_auto_expire_after_policy_period(self):
        pytest.skip('J276 RED — retention policy enforcement untested')


class TestJ277ChildDataSafeguard:
    def test_minor_data_purge_on_age_threshold(self):
        pytest.skip('J277 RED — minor-to-adult transition: stricter '
                    'data retention should relax; journey gap')


class TestJ278CrossBorderTransfer:
    def test_data_residency_honored_for_eu_user(self):
        pytest.skip('J278 RED — data residency enforcement (EU user → '
                    'EU regional node only) journey untested')


class TestJ279Anonymization:
    def test_secret_redactor_used_on_every_egress(self):
        skip_if_missing('security.secret_redactor:redact_experience')
        pytest.skip(
            'J279 — secret_redactor has unit coverage; E2E assertion '
            'that EVERY outbound federation delta passed through it '
            'is missing (invariant test, not journey)'
        )
