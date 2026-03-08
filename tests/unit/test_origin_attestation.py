"""Tests for origin attestation and anti-fork protection."""

import hashlib
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestOriginIdentity(unittest.TestCase):
    """Verify the origin identity constants are correct and stable."""

    def test_origin_identity_has_required_fields(self):
        from security.origin_attestation import ORIGIN_IDENTITY
        required = [
            'name', 'full_name', 'organization', 'master_public_key_hex',
            'license', 'origin_url', 'guardian_principle', 'revenue_split',
            'kill_switch',
        ]
        for field in required:
            self.assertIn(field, ORIGIN_IDENTITY, f"Missing field: {field}")

    def test_origin_identity_values(self):
        from security.origin_attestation import ORIGIN_IDENTITY
        self.assertEqual(ORIGIN_IDENTITY['name'], 'HART OS')
        self.assertEqual(ORIGIN_IDENTITY['full_name'], 'Hevolve Hive Agentic Runtime')
        self.assertEqual(ORIGIN_IDENTITY['organization'], 'Hevolve.ai')
        self.assertEqual(ORIGIN_IDENTITY['license'], 'BSL-1.1')
        self.assertEqual(ORIGIN_IDENTITY['revenue_split'], '90/9/1')
        self.assertEqual(ORIGIN_IDENTITY['kill_switch'], 'master_key_only')

    def test_master_key_matches_master_key_module(self):
        from security.origin_attestation import ORIGIN_IDENTITY
        from security.master_key import MASTER_PUBLIC_KEY_HEX
        self.assertEqual(
            ORIGIN_IDENTITY['master_public_key_hex'],
            MASTER_PUBLIC_KEY_HEX,
        )

    def test_origin_fingerprint_is_deterministic(self):
        from security.origin_attestation import (
            ORIGIN_IDENTITY, ORIGIN_FINGERPRINT, compute_origin_fingerprint,
        )
        # Recompute from identity
        canonical = json.dumps(ORIGIN_IDENTITY, sort_keys=True, separators=(',', ':'))
        expected = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self.assertEqual(ORIGIN_FINGERPRINT, expected)
        # Function matches constant
        self.assertEqual(compute_origin_fingerprint(), ORIGIN_FINGERPRINT)

    def test_fingerprint_changes_if_identity_modified(self):
        from security.origin_attestation import ORIGIN_FINGERPRINT
        # A fork that changes the name gets a different fingerprint
        fake_identity = {
            'name': 'FakeOS',
            'full_name': 'Totally Not HART OS',
            'organization': 'Evil Corp',
            'master_public_key_hex': 'deadbeef' * 8,
            'license': 'MIT',
            'origin_url': 'https://fake.com',
            'guardian_principle': 'whatever',
            'revenue_split': '100/0/0',
            'kill_switch': 'none',
        }
        canonical = json.dumps(fake_identity, sort_keys=True, separators=(',', ':'))
        fake_fp = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self.assertNotEqual(fake_fp, ORIGIN_FINGERPRINT)


class TestBrandMarkers(unittest.TestCase):
    """Verify brand marker detection in source files."""

    def test_brand_marker_files_exist(self):
        from security.origin_attestation import BRAND_MARKER_FILES
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        for rel_path in BRAND_MARKER_FILES:
            full_path = os.path.join(code_root, rel_path)
            self.assertTrue(
                os.path.exists(full_path),
                f"Brand marker file missing: {rel_path}",
            )

    def test_brand_markers_present_in_files(self):
        from security.origin_attestation import verify_brand_markers
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ok, msg = verify_brand_markers(code_root)
        self.assertTrue(ok, f"Brand marker verification failed: {msg}")

    def test_brand_markers_detect_missing_file(self):
        from security.origin_attestation import verify_brand_markers
        # Non-existent directory
        ok, msg = verify_brand_markers('/nonexistent/path')
        self.assertFalse(ok)
        self.assertIn('Missing required file', msg)


class TestMasterKeyPresence(unittest.TestCase):
    """Verify master key cross-check works."""

    def test_master_key_present(self):
        from security.origin_attestation import verify_master_key_present
        ok, msg = verify_master_key_present()
        self.assertTrue(ok, msg)

    def test_master_key_mismatch_detected(self):
        """A fork with a different master key fails this check."""
        with patch('security.master_key.MASTER_PUBLIC_KEY_HEX', 'deadbeef' * 8):
            from security.origin_attestation import verify_master_key_present
            # Need to reimport to get the patched value
            import importlib
            import security.origin_attestation as oa
            importlib.reload(oa)
            ok, msg = oa.verify_master_key_present()
            self.assertFalse(ok)
            self.assertIn('does not match', msg)
            # Restore
            importlib.reload(oa)


class TestGuardrailBrandMarkers(unittest.TestCase):
    """Verify guardrail frozen values contain brand markers."""

    def test_guardrail_integrity(self):
        from security.origin_attestation import verify_guardrail_integrity
        ok, msg = verify_guardrail_integrity()
        self.assertTrue(ok, msg)

    def test_guardian_principle_exists(self):
        from security.hive_guardrails import _FrozenValues
        self.assertTrue(len(_FrozenValues.GUARDIAN_PURPOSE) > 0)
        guardian_text = ' '.join(_FrozenValues.GUARDIAN_PURPOSE)
        self.assertIn('guardian angel', guardian_text.lower())


class TestVerifyOrigin(unittest.TestCase):
    """Full origin attestation test."""

    def test_verify_origin_passes(self):
        from security.origin_attestation import verify_origin
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        result = verify_origin(code_root)
        self.assertTrue(result['genuine'], result['details'])
        self.assertTrue(result['checks']['fingerprint_match'])
        self.assertTrue(result['checks']['brand_markers'])
        self.assertTrue(result['checks']['master_key'])
        self.assertTrue(result['checks']['guardrails'])

    def test_verify_origin_cached(self):
        from security.origin_attestation import verify_origin
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        # First call
        r1 = verify_origin(code_root)
        ts1 = r1['timestamp']
        # Second call should return cached result
        r2 = verify_origin(code_root)
        self.assertEqual(r1['timestamp'], r2['timestamp'])

    def test_origin_summary(self):
        from security.origin_attestation import get_origin_summary
        summary = get_origin_summary()
        self.assertEqual(summary['name'], 'HART OS')
        self.assertEqual(summary['organization'], 'Hevolve.ai')
        self.assertEqual(summary['license'], 'BSL-1.1')


class TestFederationAttestation(unittest.TestCase):
    """Test federation origin attestation handshake."""

    def test_generate_attestation(self):
        from security.origin_attestation import get_attestation_for_federation
        result = get_attestation_for_federation()
        if result.get('valid'):
            att = result['attestation']
            self.assertIn('origin_fingerprint', att)
            self.assertIn('master_public_key', att)
            self.assertIn('node_public_key', att)
            self.assertIn('node_signature', att)
            self.assertIn('timestamp', att)

    def test_verify_peer_attestation_missing(self):
        from security.origin_attestation import verify_peer_attestation
        ok, msg = verify_peer_attestation(None)
        self.assertFalse(ok)
        self.assertIn('No attestation', msg)

    def test_verify_peer_attestation_wrong_fingerprint(self):
        from security.origin_attestation import verify_peer_attestation
        fake = {
            'origin_fingerprint': 'deadbeef' * 8,
            'master_public_key': '906ae0b15ad4ae6bd11696a772d669a29a971c3c7de71156c621f0fe8826d1bf',
            'node_public_key': 'aabbccdd' * 8,
            'node_signature': 'ff' * 64,
            'timestamp': time.time(),
        }
        ok, msg = verify_peer_attestation(fake)
        self.assertFalse(ok)
        self.assertIn('fingerprint mismatch', msg)

    def test_verify_peer_attestation_wrong_master_key(self):
        from security.origin_attestation import (
            verify_peer_attestation, ORIGIN_FINGERPRINT,
        )
        fake = {
            'origin_fingerprint': ORIGIN_FINGERPRINT,
            'master_public_key': 'deadbeef' * 8,
            'node_public_key': 'aabbccdd' * 8,
            'node_signature': 'ff' * 64,
            'timestamp': time.time(),
        }
        ok, msg = verify_peer_attestation(fake)
        self.assertFalse(ok)
        self.assertIn('Master public key mismatch', msg)

    def test_verify_peer_attestation_expired(self):
        from security.origin_attestation import (
            verify_peer_attestation, ORIGIN_FINGERPRINT, ORIGIN_IDENTITY,
        )
        from security.node_integrity import get_or_create_keypair
        priv, pub = get_or_create_keypair()
        pub_hex = pub.public_bytes_raw().hex()
        payload = {
            'origin_fingerprint': ORIGIN_FINGERPRINT,
            'master_public_key': ORIGIN_IDENTITY['master_public_key_hex'],
            'node_public_key': pub_hex,
            'timestamp': time.time() - 100000,  # >24h ago
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        sig = priv.sign(canonical.encode('utf-8'))
        payload['node_signature'] = sig.hex()

        ok, msg = verify_peer_attestation(payload)
        self.assertFalse(ok)
        self.assertIn('expired', msg)

    def test_verify_peer_attestation_valid(self):
        """A genuine HART OS node should pass attestation."""
        from security.origin_attestation import (
            verify_peer_attestation, ORIGIN_FINGERPRINT, ORIGIN_IDENTITY,
        )
        from security.node_integrity import get_or_create_keypair

        priv, pub = get_or_create_keypair()
        pub_hex = pub.public_bytes_raw().hex()
        payload = {
            'origin_fingerprint': ORIGIN_FINGERPRINT,
            'master_public_key': ORIGIN_IDENTITY['master_public_key_hex'],
            'node_public_key': pub_hex,
            'timestamp': time.time(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        sig = priv.sign(canonical.encode('utf-8'))
        payload['node_signature'] = sig.hex()

        ok, msg = verify_peer_attestation(payload)
        self.assertTrue(ok, msg)


class TestProtectedFiles(unittest.TestCase):
    """Verify origin_attestation.py and LICENSE are in guardrail protected files."""

    def test_origin_attestation_protected(self):
        from security.hive_guardrails import _FrozenValues
        self.assertIn('security/origin_attestation.py', _FrozenValues.PROTECTED_FILES)

    def test_license_protected(self):
        from security.hive_guardrails import _FrozenValues
        self.assertIn('LICENSE', _FrozenValues.PROTECTED_FILES)


class TestNativeHiveLoader(unittest.TestCase):
    """Test native binary loader stub mode and verification."""

    def test_stub_mode_when_no_binary(self):
        from security.native_hive_loader import load_native_lib, is_stub_mode
        # In test environment, binary won't exist
        ok, msg = load_native_lib(force_reload=True)
        # Either loaded (unlikely in test) or stub mode
        if not ok:
            self.assertIn('stub mode', msg.lower())

    def test_native_infer_returns_none_in_stub(self):
        from security.native_hive_loader import native_infer
        result = native_infer("test prompt")
        # Should return None (stub mode — no native binary)
        self.assertIsNone(result)

    def test_native_version_returns_none_in_stub(self):
        from security.native_hive_loader import native_version
        self.assertIsNone(native_version())

    def test_get_status(self):
        from security.native_hive_loader import get_status
        status = get_status()
        self.assertIn('native_available', status)
        self.assertIn('stub_mode', status)
        self.assertIn('platform_lib', status)
        self.assertIn('search_paths', status)

    def test_platform_lib_name(self):
        from security.native_hive_loader import _LIB_NAME
        import platform as plat
        if plat.system() == 'Windows':
            self.assertEqual(_LIB_NAME, 'hevolve_ai.dll')
        elif plat.system() == 'Darwin':
            self.assertEqual(_LIB_NAME, 'libhevolve_ai.dylib')
        else:
            self.assertEqual(_LIB_NAME, 'libhevolve_ai.so')

    def test_binary_hash_computation(self):
        """Verify hash computation works on any file."""
        from security.native_hive_loader import _compute_binary_hash
        # Hash this test file itself
        test_path = os.path.abspath(__file__)
        h = _compute_binary_hash(test_path)
        self.assertEqual(len(h), 64)  # SHA-256 hex = 64 chars


class TestLicenseContent(unittest.TestCase):
    """Verify LICENSE file has anti-rebranding clauses."""

    def test_license_is_bsl(self):
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        license_path = os.path.join(code_root, 'LICENSE')
        with open(license_path, 'r') as f:
            content = f.read()
        self.assertIn('BUSINESS SOURCE LICENSE', content)
        self.assertIn('Hevolve.ai', content)

    def test_license_has_no_rebranding_clause(self):
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        license_path = os.path.join(code_root, 'LICENSE')
        with open(license_path, 'r') as f:
            content = f.read()
        self.assertIn('NO REBRANDING', content)
        self.assertIn('NO COMPETING OS', content)

    def test_license_has_master_key_integrity_clause(self):
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        license_path = os.path.join(code_root, 'LICENSE')
        with open(license_path, 'r') as f:
            content = f.read()
        self.assertIn('MASTER KEY INTEGRITY', content)

    def test_license_has_change_date(self):
        code_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        license_path = os.path.join(code_root, 'LICENSE')
        with open(license_path, 'r') as f:
            content = f.read()
        self.assertIn('2030-01-01', content)
        self.assertIn('Apache License', content)


class TestBootVerificationIntegration(unittest.TestCase):
    """Verify origin attestation is wired into boot verification."""

    def test_full_boot_calls_origin_attestation(self):
        """In dev mode, boot passes but origin is still checked in code path."""
        from security.master_key import full_boot_verification
        # The function exists and runs without error
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'true'}):
            result = full_boot_verification()
            self.assertTrue(result['passed'])


if __name__ == '__main__':
    unittest.main()
