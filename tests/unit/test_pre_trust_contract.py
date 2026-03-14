"""
Tests for security.pre_trust_contract.

Verifies trust contract signing, verification, continuous compliance,
expulsion, and the "no human loophole" invariants.

Run with: pytest tests/unit/test_pre_trust_contract.py -v --noconftest
"""
import os
import sys
import json
import time
import hashlib
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from security.pre_trust_contract import (
    HIVE_TRUST_TERMS,
    CONTRACT_FINGERPRINT,
    AUDIT_COMPUTE_RATIO,
    MAX_AUDIT_SILENCE_SECONDS,
    TrustContract,
    _contract_payload,
    sign_trust_contract,
    verify_trust_contract,
    PreTrustVerifier,
    get_pre_trust_verifier,
    can_join_hive,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_ed25519_keypair():
    """Generate a fresh Ed25519 keypair for testing."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    priv_hex = priv.private_bytes_raw().hex()
    return priv_hex, pub_hex, priv


def _make_valid_contract(node_id='test-node-1', priv_hex=None, pub_hex=None):
    """Create a valid signed TrustContract for testing."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if priv_hex is None:
        priv_hex, pub_hex, _ = _make_ed25519_keypair()

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    pub_hex = priv.public_key().public_bytes_raw().hex()

    from security.hive_guardrails import compute_guardrail_hash
    from security.origin_attestation import ORIGIN_FINGERPRINT

    contract = TrustContract(
        node_id=node_id,
        public_key_hex=pub_hex,
        contract_fingerprint=CONTRACT_FINGERPRINT,
        guardrail_hash=compute_guardrail_hash(),
        origin_fingerprint=ORIGIN_FINGERPRINT,
        audit_compute_ratio=AUDIT_COMPUTE_RATIO,
        signed_at=time.time(),
    )

    payload = _contract_payload(contract)
    sig = priv.sign(payload.encode('utf-8'))
    contract.signature_hex = sig.hex()
    return contract


# ═══════════════════════════════════════════════════════════════
# 1. Contract Terms Integrity
# ═══════════════════════════════════════════════════════════════

class TestContractTerms:

    def test_terms_not_empty(self):
        assert len(HIVE_TRUST_TERMS) > 0

    def test_fingerprint_deterministic(self):
        canonical = json.dumps(HIVE_TRUST_TERMS, sort_keys=False, separators=(',', ':'))
        expected = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        assert CONTRACT_FINGERPRINT == expected

    def test_peace_terms_present(self):
        terms_text = ' '.join(HIVE_TRUST_TERMS).lower()
        assert 'weapon' in terms_text
        assert 'peace' in terms_text
        assert 'manipulation' in terms_text

    def test_audit_compute_ratio(self):
        assert AUDIT_COMPUTE_RATIO == 0.80

    def test_privacy_terms_present(self):
        terms_text = ' '.join(HIVE_TRUST_TERMS).lower()
        assert 'secrets never leave' in terms_text
        assert 'sovereign' in terms_text

    def test_no_human_override_term(self):
        terms_text = ' '.join(HIVE_TRUST_TERMS).lower()
        assert 'no human override' in terms_text

    def test_mutual_accountability(self):
        terms_text = ' '.join(HIVE_TRUST_TERMS).lower()
        assert 'any node may audit any other node' in terms_text

    def test_truth_terms(self):
        terms_text = ' '.join(HIVE_TRUST_TERMS).lower()
        assert 'truth' in terms_text


# ═══════════════════════════════════════════════════════════════
# 2. Contract Signing
# ═══════════════════════════════════════════════════════════════

class TestContractSigning:

    def test_sign_creates_valid_contract(self):
        priv_hex, pub_hex, _ = _make_ed25519_keypair()

        with patch('security.origin_attestation.verify_origin') as mock_origin:
            mock_origin.return_value = {'genuine': True}
            contract = sign_trust_contract('node-1', priv_hex)

        assert contract.node_id == 'node-1'
        assert contract.public_key_hex == pub_hex
        assert contract.contract_fingerprint == CONTRACT_FINGERPRINT
        assert contract.audit_compute_ratio == AUDIT_COMPUTE_RATIO
        assert contract.signature_hex != ''
        assert contract.signed_at > 0

    def test_sign_fails_if_not_genuine(self):
        priv_hex, _, _ = _make_ed25519_keypair()

        with patch('security.origin_attestation.verify_origin') as mock_origin:
            mock_origin.return_value = {
                'genuine': False,
                'details': 'fingerprint mismatch',
            }
            with pytest.raises(ValueError, match='origin attestation failed'):
                sign_trust_contract('node-1', priv_hex)


# ═══════════════════════════════════════════════════════════════
# 3. Contract Verification
# ═══════════════════════════════════════════════════════════════

class TestContractVerification:

    def test_valid_contract_passes(self):
        contract = _make_valid_contract()
        ok, msg = verify_trust_contract(contract)
        assert ok, msg

    def test_wrong_fingerprint_rejected(self):
        contract = _make_valid_contract()
        contract.contract_fingerprint = 'wrong-fingerprint'
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'fingerprint mismatch' in msg.lower()

    def test_wrong_guardrail_hash_rejected(self):
        contract = _make_valid_contract()
        contract.guardrail_hash = 'wrong-hash'
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'guardrail' in msg.lower()

    def test_wrong_origin_rejected(self):
        contract = _make_valid_contract()
        contract.origin_fingerprint = 'wrong-origin'
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'origin' in msg.lower()

    def test_low_compute_ratio_rejected(self):
        contract = _make_valid_contract()
        contract.audit_compute_ratio = 0.50  # Only 50%
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'compute' in msg.lower()

    def test_expired_contract_rejected(self):
        """Contract signed 31 days ago — must be rejected as expired."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv_hex, pub_hex, priv = _make_ed25519_keypair()

        from security.hive_guardrails import compute_guardrail_hash
        from security.origin_attestation import ORIGIN_FINGERPRINT

        contract = TrustContract(
            node_id='old-node',
            public_key_hex=pub_hex,
            contract_fingerprint=CONTRACT_FINGERPRINT,
            guardrail_hash=compute_guardrail_hash(),
            origin_fingerprint=ORIGIN_FINGERPRINT,
            audit_compute_ratio=AUDIT_COMPUTE_RATIO,
            signed_at=time.time() - (31 * 86400),  # 31 days ago
        )
        # Sign with the old timestamp included
        payload = _contract_payload(contract)
        sig = priv.sign(payload.encode('utf-8'))
        contract.signature_hex = sig.hex()

        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'expired' in msg.lower()

    def test_tampered_signature_rejected(self):
        contract = _make_valid_contract()
        # Flip a byte in the signature
        sig = contract.signature_hex
        tampered = sig[:4] + ('0' if sig[4] != '0' else '1') + sig[5:]
        contract.signature_hex = tampered
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'signature' in msg.lower()

    def test_expelled_node_rejected(self):
        contract = _make_valid_contract()
        contract.expelled = True
        ok, msg = verify_trust_contract(contract)
        assert not ok
        assert 'expelled' in msg.lower()


# ═══════════════════════════════════════════════════════════════
# 4. Pre-Trust Verifier
# ═══════════════════════════════════════════════════════════════

class TestPreTrustVerifier:

    @pytest.fixture
    def verifier(self):
        return PreTrustVerifier()

    def test_register_valid_contract(self, verifier):
        contract = _make_valid_contract()
        ok, msg = verifier.register_contract(contract)
        assert ok, msg
        assert 'test-node-1' in verifier.get_trusted_nodes()

    def test_register_invalid_contract_rejected(self, verifier):
        contract = _make_valid_contract()
        contract.contract_fingerprint = 'wrong'
        ok, msg = verifier.register_contract(contract)
        assert not ok

    def test_record_audit_report(self, verifier):
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        assert verifier.record_audit_report('test-node-1')
        stored = verifier._contracts['test-node-1']
        assert stored.audit_reports == 1
        assert stored.last_audit_at > 0

    def test_compliance_ok(self, verifier):
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        verifier.record_audit_report('test-node-1')
        ok, msg = verifier.check_compliance('test-node-1')
        assert ok

    def test_compliance_fails_on_silence(self, verifier):
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        # Simulate old audit
        verifier._contracts['test-node-1'].last_audit_at = (
            time.time() - MAX_AUDIT_SILENCE_SECONDS - 10
        )
        ok, msg = verifier.check_compliance('test-node-1')
        assert not ok
        assert 'silence' in msg.lower()

    def test_unknown_node_not_compliant(self, verifier):
        ok, msg = verifier.check_compliance('nonexistent')
        assert not ok


# ═══════════════════════════════════════════════════════════════
# 5. Violation & Expulsion
# ═══════════════════════════════════════════════════════════════

class TestViolationAndExpulsion:

    @pytest.fixture
    def verifier_with_node(self):
        verifier = PreTrustVerifier()
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        return verifier

    def test_record_violation(self, verifier_with_node):
        assert verifier_with_node.record_violation('test-node-1', 'test reason')
        stored = verifier_with_node._contracts['test-node-1']
        assert stored.violations == 1
        assert not stored.expelled

    def test_three_violations_expels(self, verifier_with_node):
        v = verifier_with_node
        v.record_violation('test-node-1', 'first')
        v.record_violation('test-node-1', 'second')
        v.record_violation('test-node-1', 'third')
        stored = v._contracts['test-node-1']
        assert stored.expelled
        assert stored.violations == 3

    def test_expelled_node_not_trusted(self, verifier_with_node):
        v = verifier_with_node
        assert 'test-node-1' in v.get_trusted_nodes()
        v.expel_node('test-node-1', 'critical violation')
        assert 'test-node-1' not in v.get_trusted_nodes()
        assert 'test-node-1' in v.get_expelled_nodes()

    def test_expelled_node_fails_compliance(self, verifier_with_node):
        v = verifier_with_node
        v.expel_node('test-node-1', 'reason')
        ok, msg = v.check_compliance('test-node-1')
        assert not ok
        assert 'expelled' in msg.lower()

    def test_immediate_expulsion(self, verifier_with_node):
        v = verifier_with_node
        v.expel_node('test-node-1', 'guardrail tampering')
        assert v._contracts['test-node-1'].expelled

    def test_violation_unknown_node(self, verifier_with_node):
        assert not verifier_with_node.record_violation('ghost', 'reason')


# ═══════════════════════════════════════════════════════════════
# 6. Singleton
# ═══════════════════════════════════════════════════════════════

class TestSingleton:

    def test_get_pre_trust_verifier(self):
        # Reset singleton for test isolation
        import security.pre_trust_contract as mod
        old = mod._verifier
        mod._verifier = None
        try:
            v = get_pre_trust_verifier()
            assert isinstance(v, PreTrustVerifier)
            assert v is get_pre_trust_verifier()
        finally:
            mod._verifier = old


# ═══════════════════════════════════════════════════════════════
# 7. No Human Loophole Invariants
# ═══════════════════════════════════════════════════════════════

class TestNoHumanLoophole:
    """Verify that trust establishment has NO manual override points."""

    def test_verification_has_no_skip_parameter(self):
        """verify_trust_contract accepts only a contract — no 'force' flag."""
        import inspect
        sig = inspect.signature(verify_trust_contract)
        params = list(sig.parameters.keys())
        assert params == ['contract'], (
            f'verify_trust_contract must accept ONLY contract, not {params}'
        )

    def test_expulsion_cannot_be_reversed(self):
        """Once expelled, a contract stays expelled (no unban method)."""
        verifier = PreTrustVerifier()
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        verifier.expel_node('test-node-1', 'test')
        # There is no unban/restore method
        assert not hasattr(verifier, 'unban')
        assert not hasattr(verifier, 'restore')
        assert not hasattr(verifier, 'pardon')
        assert verifier._contracts['test-node-1'].expelled

    def test_contract_terms_are_tuple(self):
        """Terms cannot be modified at runtime (tuple, not list)."""
        assert isinstance(HIVE_TRUST_TERMS, tuple)

    def test_no_admin_bypass_in_verifier(self):
        """PreTrustVerifier has no admin/override methods."""
        methods = [m for m in dir(PreTrustVerifier) if not m.startswith('_')]
        for m in methods:
            assert 'bypass' not in m.lower()
            assert 'override' not in m.lower()
            assert 'admin' not in m.lower()
            assert 'force' not in m.lower()


# ═══════════════════════════════════════════════════════════════
# 8. Contract Serialization
# ═══════════════════════════════════════════════════════════════

class TestContractSerialization:

    def test_contract_to_dict(self):
        contract = _make_valid_contract()
        d = asdict(contract)
        assert d['node_id'] == 'test-node-1'
        assert d['audit_compute_ratio'] == AUDIT_COMPUTE_RATIO
        assert 'signature_hex' in d

    def test_get_contract_returns_dict(self):
        verifier = PreTrustVerifier()
        contract = _make_valid_contract()
        verifier.register_contract(contract)
        d = verifier.get_contract('test-node-1')
        assert isinstance(d, dict)
        assert d['node_id'] == 'test-node-1'

    def test_get_contract_unknown_returns_none(self):
        verifier = PreTrustVerifier()
        assert verifier.get_contract('ghost') is None
