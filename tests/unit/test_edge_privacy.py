"""
Tests for security.edge_privacy — Scope-based data protection at the edge.

Verifies:
  - PrivacyScope hierarchy (EDGE_ONLY < USER_DEVICES < TRUSTED_PEER < FEDERATED < PUBLIC)
  - ScopeGuard egress checks (scope enforcement + DLP delegation)
  - Redaction by scope level
  - Governance integration (privacy scorer)
  - No parallel paths — delegates to existing DLP/secret_redactor

Run with: pytest tests/unit/test_edge_privacy.py -v --noconftest
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from security.edge_privacy import (
    PrivacyScope,
    ScopeGuard,
    scope_allows,
    score_privacy,
    get_scope_guard,
)


# ═══════════════════════════════════════════════════════════════
# 1. Scope Hierarchy
# ═══════════════════════════════════════════════════════════════

class TestScopeHierarchy:

    def test_edge_only_stays_on_edge(self):
        assert scope_allows(PrivacyScope.EDGE_ONLY, PrivacyScope.EDGE_ONLY)
        assert not scope_allows(PrivacyScope.EDGE_ONLY, PrivacyScope.USER_DEVICES)
        assert not scope_allows(PrivacyScope.EDGE_ONLY, PrivacyScope.FEDERATED)
        assert not scope_allows(PrivacyScope.EDGE_ONLY, PrivacyScope.PUBLIC)

    def test_user_devices_up_to_user_devices(self):
        assert scope_allows(PrivacyScope.USER_DEVICES, PrivacyScope.EDGE_ONLY)
        assert scope_allows(PrivacyScope.USER_DEVICES, PrivacyScope.USER_DEVICES)
        assert not scope_allows(PrivacyScope.USER_DEVICES, PrivacyScope.TRUSTED_PEER)

    def test_federated_flows_down(self):
        assert scope_allows(PrivacyScope.FEDERATED, PrivacyScope.EDGE_ONLY)
        assert scope_allows(PrivacyScope.FEDERATED, PrivacyScope.FEDERATED)
        assert not scope_allows(PrivacyScope.FEDERATED, PrivacyScope.PUBLIC)

    def test_public_goes_anywhere(self):
        for scope in PrivacyScope:
            assert scope_allows(PrivacyScope.PUBLIC, scope)

    def test_scope_is_string_enum(self):
        assert PrivacyScope.EDGE_ONLY.value == 'edge_only'
        assert PrivacyScope('federated') == PrivacyScope.FEDERATED


# ═══════════════════════════════════════════════════════════════
# 2. ScopeGuard — Egress Checks
# ═══════════════════════════════════════════════════════════════

class TestScopeGuard:

    @pytest.fixture
    def guard(self):
        return ScopeGuard()

    def test_edge_data_blocked_from_federation(self, guard):
        data = {'secret': 'my-api-key', '_privacy_scope': PrivacyScope.EDGE_ONLY}
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert not ok
        assert 'scope violation' in reason.lower()

    def test_federated_data_allowed_to_federated(self, guard):
        data = {'pattern': 'common recipe', '_privacy_scope': PrivacyScope.FEDERATED}
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert ok

    def test_public_data_goes_anywhere(self, guard):
        data = {'greeting': 'hello', '_privacy_scope': PrivacyScope.PUBLIC}
        ok, _ = guard.check_egress(data, PrivacyScope.PUBLIC)
        assert ok
        ok, _ = guard.check_egress(data, PrivacyScope.EDGE_ONLY)
        assert ok

    def test_default_scope_is_edge_only(self, guard):
        """No _privacy_scope tag → treated as EDGE_ONLY (most restrictive)."""
        data = {'content': 'untagged data'}
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert not ok
        assert 'edge_only' in reason.lower()

    def test_string_scope_accepted(self, guard):
        data = {'content': 'hello', '_privacy_scope': 'public'}
        ok, _ = guard.check_egress(data, PrivacyScope.PUBLIC)
        assert ok

    def test_unknown_scope_string_defaults_to_edge(self, guard):
        data = {'content': 'hello', '_privacy_scope': 'mystery_scope'}
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert not ok

    def test_dlp_catches_pii_in_federated(self, guard):
        """Even if scope says FEDERATED, DLP blocks undeclared PII."""
        data = {
            'message': 'Contact me at john@example.com',
            '_privacy_scope': PrivacyScope.FEDERATED,
        }
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert not ok
        assert 'pii' in reason.lower() or 'email' in reason.lower()

    def test_dlp_not_needed_for_edge(self, guard):
        """EDGE_ONLY → EDGE_ONLY doesn't need DLP scan (stays on device)."""
        data = {
            'message': 'Contact me at john@example.com',
            '_privacy_scope': PrivacyScope.EDGE_ONLY,
        }
        ok, _ = guard.check_egress(data, PrivacyScope.EDGE_ONLY)
        assert ok  # Stays on device, no DLP needed


# ═══════════════════════════════════════════════════════════════
# 3. Scope Redaction
# ═══════════════════════════════════════════════════════════════

class TestScopeRedaction:

    @pytest.fixture
    def guard(self):
        return ScopeGuard()

    def test_redact_removes_edge_fields_for_federation(self, guard):
        data = {
            'public_name': 'alice',
            '_scope_public_name': PrivacyScope.PUBLIC,
            'biometric_hash': 'abc123',
            '_scope_biometric_hash': PrivacyScope.EDGE_ONLY,
            '_privacy_scope': PrivacyScope.EDGE_ONLY,
        }
        redacted = guard.redact_for_scope(data, PrivacyScope.FEDERATED)
        assert redacted['public_name'] == 'alice'
        assert 'SCOPE_REDACTED' in redacted['biometric_hash']

    def test_redact_keeps_all_for_edge(self, guard):
        data = {
            'secret': 'my_key',
            '_scope_secret': PrivacyScope.EDGE_ONLY,
            '_privacy_scope': PrivacyScope.EDGE_ONLY,
        }
        redacted = guard.redact_for_scope(data, PrivacyScope.EDGE_ONLY)
        assert redacted['secret'] == 'my_key'

    def test_redact_never_mutates_original(self, guard):
        data = {'value': 'original', '_privacy_scope': PrivacyScope.EDGE_ONLY}
        _ = guard.redact_for_scope(data, PrivacyScope.FEDERATED)
        assert data['value'] == 'original'


# ═══════════════════════════════════════════════════════════════
# 4. Governance Integration
# ═══════════════════════════════════════════════════════════════

class TestGovernanceIntegration:

    def test_no_data_is_neutral(self):
        from security.ai_governance import ConstitutionalSignal
        sig = score_privacy({})
        assert sig.score == 1.0

    def test_valid_scope_scores_high(self):
        sig = score_privacy({
            'data': {'msg': 'hello', '_privacy_scope': PrivacyScope.PUBLIC},
            'destination_scope': 'public',
        })
        assert sig.score == 1.0

    def test_scope_violation_scores_near_zero(self):
        sig = score_privacy({
            'data': {'secret': 'key123', '_privacy_scope': PrivacyScope.EDGE_ONLY},
            'destination_scope': 'federated',
        })
        assert sig.score < 0.1

    def test_pii_in_federated_scores_near_zero(self):
        sig = score_privacy({
            'data': {
                'msg': 'Call me at 555-123-4567',
                '_privacy_scope': PrivacyScope.FEDERATED,
            },
            'destination_scope': 'federated',
        })
        assert sig.score < 0.1

    def test_unknown_scope_is_uncertain(self):
        sig = score_privacy({
            'data': {'msg': 'test'},
            'destination_scope': 'invalid_scope',
        })
        assert sig.score == 0.5
        assert sig.confidence < 0.5

    def test_pipeline_has_privacy_scorer(self):
        from security.ai_governance import (
            create_default_pipeline, DecisionDomain, DecisionOutcome,
        )
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.PRIVACY.value, {
            'data': {'msg': 'hello', '_privacy_scope': PrivacyScope.PUBLIC},
            'destination_scope': 'public',
        })
        assert d.outcome == DecisionOutcome.APPROVED.value

    def test_pipeline_blocks_scope_violation(self):
        from security.ai_governance import (
            create_default_pipeline, DecisionDomain, DecisionOutcome,
        )
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.PRIVACY.value, {
            'data': {'secret': 'api_key_here', '_privacy_scope': PrivacyScope.EDGE_ONLY},
            'destination_scope': 'federated',
        })
        assert d.outcome == DecisionOutcome.REJECTED.value


# ═══════════════════════════════════════════════════════════════
# 5. Singleton & DRY
# ═══════════════════════════════════════════════════════════════

class TestSingletonAndDRY:

    def test_singleton(self):
        g1 = get_scope_guard()
        g2 = get_scope_guard()
        assert g1 is g2

    def test_guard_delegates_to_dlp(self):
        """ScopeGuard uses DLP engine — does NOT duplicate PII patterns."""
        guard = ScopeGuard()
        # This works because guard delegates to dlp_engine.get_dlp_engine()
        data = {
            'msg': 'SSN is 123-45-6789',
            '_privacy_scope': PrivacyScope.FEDERATED,
        }
        ok, reason = guard.check_egress(data, PrivacyScope.FEDERATED)
        assert not ok  # DLP catches the SSN

    def test_no_duplicate_pii_patterns(self):
        """edge_privacy.py must NOT define its own PII regex — DRY."""
        import security.edge_privacy as mod
        source = open(mod.__file__, encoding='utf-8').read()
        assert 'PII_PATTERNS' not in source
        assert r'\b\d{3}-\d{2}-\d{4}' not in source  # No SSN regex
        assert r'@[A-Za-z0-9' not in source  # No email regex
