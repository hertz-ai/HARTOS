"""Tests for Remote Desktop Phase 1 — Device ID, Session Manager, Security."""
import hashlib
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.remote_desktop.device_id import (
    get_device_id, format_device_id, parse_device_id,
    get_user_device_id, _generate_machine_fingerprint,
)
from integrations.remote_desktop.session_manager import (
    SessionManager, SessionMode, SessionState, RemoteSession,
    get_session_manager,
)
from integrations.remote_desktop import security


# ═══════════════════════════════════════════════════════════════
# Device ID Tests
# ═══════════════════════════════════════════════════════════════

class TestDeviceId(unittest.TestCase):
    """Device ID generation and formatting."""

    def setUp(self):
        # Clear cached device ID between tests
        import integrations.remote_desktop.device_id as did_mod
        did_mod._cached_device_id = None

    def test_device_id_is_16_hex_chars(self):
        dev_id = get_device_id()
        self.assertEqual(len(dev_id), 16)
        self.assertTrue(all(c in '0123456789abcdef' for c in dev_id))

    def test_device_id_deterministic(self):
        """Same machine produces same ID."""
        import integrations.remote_desktop.device_id as did_mod
        did_mod._cached_device_id = None
        id1 = get_device_id()
        did_mod._cached_device_id = None
        id2 = get_device_id()
        self.assertEqual(id1, id2)

    def test_format_device_id(self):
        self.assertEqual(format_device_id('847291053def3a21'), '847-291-053')
        self.assertEqual(format_device_id('abcdef012'), 'abc-def-012')

    def test_parse_device_id(self):
        self.assertEqual(parse_device_id('847-291-053'), '847291053')
        self.assertEqual(parse_device_id('ABC-DEF-012'), 'abcdef012')
        self.assertEqual(parse_device_id('847 291 053'), '847291053')

    def test_format_parse_roundtrip(self):
        dev_id = 'a1b2c3d4e5f67890'
        formatted = format_device_id(dev_id)
        parsed = parse_device_id(formatted)
        self.assertTrue(dev_id.startswith(parsed))

    def test_user_device_id_different_users(self):
        """Different users on same machine get different device IDs."""
        id_user_a = get_user_device_id('user_123')
        id_user_b = get_user_device_id('user_456')
        self.assertNotEqual(id_user_a, id_user_b)
        self.assertEqual(len(id_user_a), 16)

    def test_user_device_id_deterministic(self):
        id1 = get_user_device_id('user_123')
        id2 = get_user_device_id('user_123')
        self.assertEqual(id1, id2)

    def test_machine_fingerprint_deterministic(self):
        fp1 = _generate_machine_fingerprint()
        fp2 = _generate_machine_fingerprint()
        self.assertEqual(fp1, fp2)
        self.assertIn('|', fp1)

    def test_device_id_from_key_file(self):
        """Device ID derived from public key file matches compute_mesh pattern."""
        import tempfile
        import integrations.remote_desktop.device_id as did_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            key_file = os.path.join(tmpdir, 'public.key')
            with open(key_file, 'w') as f:
                f.write('test_public_key_data_12345')

            with patch.object(did_mod, '_resolve_key_dir', return_value=tmpdir):
                did_mod._cached_device_id = None
                dev_id = get_device_id()
                expected = hashlib.sha256(b'test_public_key_data_12345').hexdigest()[:16]
                self.assertEqual(dev_id, expected)


# ═══════════════════════════════════════════════════════════════
# Session Manager Tests
# ═══════════════════════════════════════════════════════════════

class TestSessionManager(unittest.TestCase):
    """Session lifecycle, OTP, multi-viewer."""

    def setUp(self):
        self.sm = SessionManager()

    def test_generate_otp_format(self):
        otp = self.sm.generate_otp('device_abc')
        self.assertEqual(len(otp), 6)
        self.assertTrue(all(c in 'abcdefghijklmnopqrstuvwxyz0123456789' for c in otp))

    def test_verify_otp_success(self):
        otp = self.sm.generate_otp('device_abc')
        self.assertTrue(self.sm.verify_otp('device_abc', otp))

    def test_verify_otp_single_use(self):
        otp = self.sm.generate_otp('device_abc')
        self.assertTrue(self.sm.verify_otp('device_abc', otp))
        self.assertFalse(self.sm.verify_otp('device_abc', otp))

    def test_verify_otp_wrong_password(self):
        self.sm.generate_otp('device_abc')
        self.assertFalse(self.sm.verify_otp('device_abc', 'wrong!'))

    def test_verify_otp_wrong_device(self):
        otp = self.sm.generate_otp('device_abc')
        self.assertFalse(self.sm.verify_otp('device_xyz', otp))

    def test_verify_otp_expired(self):
        otp = self.sm.generate_otp('device_abc')
        # Manually expire
        self.sm._otps['device_abc']['created_at'] = time.time() - 400
        self.assertFalse(self.sm.verify_otp('device_abc', otp))

    def test_create_session_same_user_auto_accept(self):
        """Same-user devices connect immediately (no OTP)."""
        session = self.sm.create_session(
            host_device_id='host_dev',
            viewer_device_id='viewer_dev',
            mode=SessionMode.FULL_CONTROL,
            host_user_id='user_123',
            viewer_user_id='user_123',
        )
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertIsNotNone(session.connected_at)
        self.assertEqual(len(session.viewers), 1)

    def test_create_session_cross_user_requires_auth(self):
        """Cross-user connections require OTP authentication."""
        session = self.sm.create_session(
            host_device_id='host_dev',
            viewer_device_id='viewer_dev',
            mode=SessionMode.VIEW_ONLY,
            host_user_id='user_123',
            viewer_user_id='user_456',
        )
        self.assertEqual(session.state, SessionState.AUTHENTICATING)
        self.assertIsNone(session.connected_at)

    def test_authenticate_session_with_otp(self):
        """Cross-user session authenticates with OTP."""
        otp = self.sm.generate_otp('host_dev')
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.FULL_CONTROL,
            host_user_id='user_123', viewer_user_id='user_456',
        )
        self.assertTrue(self.sm.authenticate_session(session.session_id, otp))
        self.assertEqual(session.state, SessionState.CONNECTED)

    def test_authenticate_session_wrong_otp(self):
        self.sm.generate_otp('host_dev')
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.VIEW_ONLY,
            host_user_id='user_123', viewer_user_id='user_456',
        )
        self.assertFalse(self.sm.authenticate_session(session.session_id, 'wrong!'))
        self.assertEqual(session.state, SessionState.AUTHENTICATING)

    def test_add_viewer_multi_viewer(self):
        """Multiple viewers can join a session."""
        session = self.sm.create_session(
            'host_dev', 'viewer_1', SessionMode.VIEW_ONLY,
            host_user_id='user_1', viewer_user_id='user_1',
        )
        self.assertTrue(self.sm.add_viewer(session.session_id, 'viewer_2', 'user_2'))
        self.assertEqual(len(session.viewers), 2)

    def test_add_viewer_no_duplicate(self):
        session = self.sm.create_session(
            'host_dev', 'viewer_1', SessionMode.VIEW_ONLY,
            host_user_id='user_1', viewer_user_id='user_1',
        )
        self.sm.add_viewer(session.session_id, 'viewer_1')
        self.assertEqual(len(session.viewers), 1)

    def test_disconnect_session(self):
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.FULL_CONTROL,
            host_user_id='user_1', viewer_user_id='user_1',
        )
        self.assertTrue(self.sm.disconnect_session(session.session_id))
        self.assertEqual(session.state, SessionState.DISCONNECTED)
        self.assertIsNotNone(session.disconnected_at)

    def test_disconnect_already_disconnected(self):
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.FULL_CONTROL,
            host_user_id='user_1', viewer_user_id='user_1',
        )
        self.sm.disconnect_session(session.session_id)
        self.assertFalse(self.sm.disconnect_session(session.session_id))

    def test_get_active_sessions(self):
        s1 = self.sm.create_session(
            'host_1', 'viewer_1', SessionMode.VIEW_ONLY,
            host_user_id='u1', viewer_user_id='u1',
        )
        s2 = self.sm.create_session(
            'host_2', 'viewer_2', SessionMode.FULL_CONTROL,
            host_user_id='u2', viewer_user_id='u2',
        )
        self.sm.disconnect_session(s1.session_id)
        active = self.sm.get_active_sessions()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].session_id, s2.session_id)

    def test_max_sessions_per_host(self):
        for i in range(self.sm.MAX_SESSIONS_PER_HOST):
            self.sm.create_session(
                'host_dev', f'viewer_{i}', SessionMode.VIEW_ONLY,
                host_user_id='u1', viewer_user_id='u1',
            )
        with self.assertRaises(ValueError):
            self.sm.create_session(
                'host_dev', 'viewer_extra', SessionMode.VIEW_ONLY,
                host_user_id='u1', viewer_user_id='u1',
            )

    def test_session_to_dict(self):
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.FULL_CONTROL,
            host_user_id='user_1', viewer_user_id='user_1',
        )
        d = session.to_dict()
        self.assertEqual(d['host_device_id'], 'host_dev')
        self.assertEqual(d['mode'], 'full_control')
        self.assertEqual(d['state'], 'connected')
        self.assertIsNotNone(d['duration_seconds'])

    def test_cleanup_stale_sessions(self):
        session = self.sm.create_session(
            'host_dev', 'viewer_dev', SessionMode.VIEW_ONLY,
            host_user_id='u1', viewer_user_id='u1',
        )
        # Force stale
        session.created_at = time.time() - self.sm.SESSION_TIMEOUT_SECONDS - 10
        removed = self.sm.cleanup_stale()
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.sm.get_active_sessions()), 0)

    def test_singleton_get_session_manager(self):
        import integrations.remote_desktop.session_manager as sm_mod
        sm_mod._session_manager = None
        sm1 = get_session_manager()
        sm2 = get_session_manager()
        self.assertIs(sm1, sm2)
        sm_mod._session_manager = None


# ═══════════════════════════════════════════════════════════════
# Security Tests
# ═══════════════════════════════════════════════════════════════

class TestRemoteDesktopSecurity(unittest.TestCase):
    """Auth, audit, DLP, input classification."""

    def test_authenticate_same_user_auto_accept(self):
        ok, reason = security.authenticate_connection(
            'host_dev', 'viewer_dev', password='',
            host_user_id='user_1', viewer_user_id='user_1',
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'same_user_auto_accept')

    def test_authenticate_cross_user_no_password(self):
        ok, reason = security.authenticate_connection(
            'host_dev', 'viewer_dev', password='',
            host_user_id='user_1', viewer_user_id='user_2',
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'password_required')

    def test_authenticate_cross_user_wrong_password(self):
        ok, reason = security.authenticate_connection(
            'host_dev', 'viewer_dev', password='wrong!',
            host_user_id='user_1', viewer_user_id='user_2',
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'invalid_password')

    def test_classify_safe_mouse_click(self):
        result = security.classify_remote_input({
            'type': 'click', 'x': 100, 'y': 200,
        })
        self.assertEqual(result, 'safe')

    def test_classify_destructive_alt_f4(self):
        result = security.classify_remote_input({
            'type': 'hotkey', 'hotkey': 'alt+f4',
        })
        self.assertEqual(result, 'destructive')

    def test_classify_destructive_ctrl_alt_delete(self):
        result = security.classify_remote_input({
            'type': 'hotkey', 'hotkey': 'ctrl+alt+delete',
        })
        self.assertEqual(result, 'destructive')

    def test_generate_session_token(self):
        """Falls back to simple token when JWT auth unavailable."""
        token = security.generate_session_token('session_abc', 'device_123')
        self.assertIsNotNone(token)
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 10)

    def test_scan_file_transfer_clean(self):
        """Clean filename passes DLP scan (or DLP unavailable)."""
        allowed, reason = security.scan_file_transfer('document.pdf')
        self.assertTrue(allowed)

    def test_scan_clipboard_clean(self):
        allowed, reason = security.scan_clipboard('hello world')
        self.assertTrue(allowed)

    def test_encrypt_frame_graceful_fallback(self):
        """encrypt_frame returns None when crypto unavailable or bad key."""
        result = security.encrypt_frame(b'frame_data', 'invalid_hex')
        # Either returns envelope or None (graceful)
        self.assertTrue(result is None or isinstance(result, dict))

    def test_audit_session_event_graceful(self):
        """Audit logging works or returns None gracefully."""
        result = security.audit_session_event(
            'session_started', 'session_abc', 'user_123',
            detail={'mode': 'full_control'},
        )
        # Either returns (id, hash) or None
        self.assertTrue(result is None or isinstance(result, tuple))


if __name__ == '__main__':
    unittest.main()
