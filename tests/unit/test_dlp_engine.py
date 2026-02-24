"""
Tests for security/dlp_engine.py — PII detection, redaction, and outbound gating.

Run: pytest tests/unit/test_dlp_engine.py -v --noconftest
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from security.dlp_engine import DLPEngine, get_dlp_engine


class TestDLPScan(unittest.TestCase):
    """PII detection tests."""

    def setUp(self):
        self.dlp = DLPEngine()

    def test_detect_email(self):
        findings = self.dlp.scan("Contact john@example.com for details")
        types = [f[0] for f in findings]
        self.assertIn('email', types)

    def test_detect_phone(self):
        findings = self.dlp.scan("Call 555-123-4567 now")
        types = [f[0] for f in findings]
        self.assertIn('phone', types)

    def test_detect_ssn(self):
        findings = self.dlp.scan("SSN: 123-45-6789")
        types = [f[0] for f in findings]
        self.assertIn('ssn', types)

    def test_detect_credit_card(self):
        findings = self.dlp.scan("Card: 4111 1111 1111 1111")
        types = [f[0] for f in findings]
        self.assertIn('credit_card', types)

    def test_no_false_positive_on_clean_text(self):
        findings = self.dlp.scan("The quick brown fox jumps over the lazy dog")
        self.assertEqual(len(findings), 0)

    def test_safe_ip_not_flagged(self):
        """127.0.0.1 and other known-safe IPs should not be flagged."""
        findings = self.dlp.scan("Connect to 127.0.0.1")
        ip_findings = [f for f in findings if f[0] == 'ip_address']
        self.assertEqual(len(ip_findings), 0)

    def test_empty_text(self):
        self.assertEqual(self.dlp.scan(""), [])
        self.assertEqual(self.dlp.scan(None), [])

    def test_multiple_pii_types(self):
        text = "Email john@test.com, SSN 123-45-6789, call 555-123-4567"
        findings = self.dlp.scan(text)
        types = set(f[0] for f in findings)
        self.assertIn('email', types)
        self.assertIn('ssn', types)
        self.assertIn('phone', types)


class TestDLPRedact(unittest.TestCase):
    """PII redaction tests."""

    def setUp(self):
        self.dlp = DLPEngine()

    def test_redact_email(self):
        result = self.dlp.redact("Send to john@example.com")
        self.assertNotIn('john@example.com', result)
        self.assertIn('[EMAIL_REDACTED]', result)

    def test_redact_ssn(self):
        result = self.dlp.redact("SSN is 123-45-6789")
        self.assertNotIn('123-45-6789', result)
        self.assertIn('[SSN_REDACTED]', result)

    def test_redact_preserves_non_pii(self):
        result = self.dlp.redact("Hello world, call 555-123-4567")
        self.assertIn('Hello world', result)

    def test_disabled_no_redaction(self):
        dlp = DLPEngine(enabled=False)
        text = "SSN: 123-45-6789"
        self.assertEqual(dlp.redact(text), text)


class TestDLPOutboundGate(unittest.TestCase):
    """Outbound data gating."""

    def test_clean_text_allowed(self):
        dlp = DLPEngine()
        allowed, reason = dlp.check_outbound("Safe text with no PII")
        self.assertTrue(allowed)
        self.assertEqual(reason, '')

    def test_pii_blocked_by_default(self):
        dlp = DLPEngine(block_on_pii=True)
        allowed, reason = dlp.check_outbound("Email: john@example.com")
        self.assertFalse(allowed)
        self.assertIn('email', reason)

    def test_pii_allowed_in_log_only_mode(self):
        dlp = DLPEngine(block_on_pii=False)
        allowed, reason = dlp.check_outbound("Email: john@example.com")
        self.assertTrue(allowed)
        self.assertIn('email', reason)  # Still reports findings


class TestDLPSingleton(unittest.TestCase):

    def test_singleton_returns_same_instance(self):
        import security.dlp_engine as mod
        mod._dlp_engine = None
        a = get_dlp_engine()
        b = get_dlp_engine()
        self.assertIs(a, b)


if __name__ == '__main__':
    unittest.main()
