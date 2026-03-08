"""
Tests for security/immutable_audit_log.py — tamper-evident hash chain.

Run: pytest tests/unit/test_immutable_audit_log.py -v --noconftest
"""
import os
import sys
import unittest
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from security.immutable_audit_log import (
    ImmutableAuditLog, _compute_hash, _redact_sensitive, get_audit_log,
)


class TestImmutableAuditLog(unittest.TestCase):
    """Core audit log functionality."""

    def setUp(self):
        # Fresh in-memory instance for each test (no DB)
        self.log = ImmutableAuditLog()
        self.log._use_db = False
        self.log._memory_log = []

    def test_log_event_returns_id_and_hash(self):
        entry_id, entry_hash = self.log.log_event(
            'state_change', actor_id='user_1', action='completed task 5')
        self.assertEqual(entry_id, 1)
        self.assertIsInstance(entry_hash, str)
        self.assertEqual(len(entry_hash), 64)  # SHA-256 hex

    def test_chain_integrity_multiple_entries(self):
        """Multiple entries form a valid chain."""
        for i in range(10):
            self.log.log_event('test', actor_id=f'user_{i}', action=f'action_{i}')

        ok, reason = self.log.verify_chain()
        self.assertTrue(ok, f"Chain should be valid: {reason}")
        self.assertIn('10 entries', reason)

    def test_tamper_detection(self):
        """Modifying an entry breaks the chain."""
        self.log.log_event('auth', actor_id='user_1', action='login')
        self.log.log_event('state_change', actor_id='user_1', action='update')
        self.log.log_event('auth', actor_id='user_1', action='logout')

        # Tamper with middle entry
        self.log._memory_log[1]['action'] = 'TAMPERED'

        ok, reason = self.log.verify_chain()
        self.assertFalse(ok, "Chain should be broken after tamper")
        self.assertIn('Chain broken', reason)

    def test_empty_chain_valid(self):
        ok, reason = self.log.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(reason, 'Empty log')

    def test_sensitive_redaction(self):
        """Sensitive fields in detail are redacted."""
        self.log.log_event(
            'auth', actor_id='user_1', action='login',
            detail={'username': 'john', 'password': 'secret123', 'token': 'abc'})

        entry = self.log._memory_log[0]
        self.assertIn('[REDACTED]', entry['detail_json'])
        self.assertNotIn('secret123', entry['detail_json'])
        self.assertIn('john', entry['detail_json'])  # username not redacted

    def test_get_trail_filters(self):
        """Trail filtering by actor and event type."""
        self.log.log_event('auth', actor_id='user_1', action='login')
        self.log.log_event('state_change', actor_id='user_2', action='update')
        self.log.log_event('auth', actor_id='user_1', action='logout')
        self.log.log_event('state_change', actor_id='user_1', action='delete')

        # Filter by actor
        trail = self.log.get_trail(actor_id='user_1')
        self.assertEqual(len(trail), 3)

        # Filter by event type
        trail = self.log.get_trail(event_type='auth')
        self.assertEqual(len(trail), 2)

        # Both filters
        trail = self.log.get_trail(actor_id='user_1', event_type='auth')
        self.assertEqual(len(trail), 2)

    def test_concurrent_writes(self):
        """Concurrent writes must not corrupt the chain."""
        errors = []

        def write_batch(prefix, count):
            try:
                for i in range(count):
                    self.log.log_event('test', actor_id=prefix, action=f'{prefix}_{i}')
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=write_batch, args=(f't{i}', 20))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent write errors: {errors}")
        self.assertEqual(len(self.log._memory_log), 100)

        ok, reason = self.log.verify_chain()
        self.assertTrue(ok, f"Chain should be valid after concurrent writes: {reason}")


class TestHashFunction(unittest.TestCase):
    """Verify hash computation is deterministic."""

    def test_deterministic_hash(self):
        h1 = _compute_hash('prev', 'type', 'actor', 'action', '2026-01-01', None)
        h2 = _compute_hash('prev', 'type', 'actor', 'action', '2026-01-01', None)
        self.assertEqual(h1, h2)

    def test_different_inputs_different_hash(self):
        h1 = _compute_hash('prev', 'type', 'actor', 'action_A', '2026-01-01', None)
        h2 = _compute_hash('prev', 'type', 'actor', 'action_B', '2026-01-01', None)
        self.assertNotEqual(h1, h2)


class TestRedactSensitive(unittest.TestCase):

    def test_redacts_password(self):
        result = _redact_sensitive({'password': 'secret', 'name': 'john'})
        self.assertIn('[REDACTED]', result)
        self.assertNotIn('secret', result)
        self.assertIn('john', result)

    def test_none_detail(self):
        self.assertIsNone(_redact_sensitive(None))


class TestSingleton(unittest.TestCase):

    def test_singleton_returns_same_instance(self):
        import security.immutable_audit_log as mod
        mod._audit_log = None
        a = get_audit_log()
        b = get_audit_log()
        self.assertIs(a, b)


if __name__ == '__main__':
    unittest.main()
