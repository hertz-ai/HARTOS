"""
Functional tests for security modules with zero or low prior test coverage.

Tests REAL implementations (real SQLite, real crypto, real filesystem) wherever
possible. No HSM hardware or external services required.

Modules covered:
  1. security/sanitize.py       — escape_like, sanitize_path, sanitize_html,
                                   validate_input, validate_prompt_id, validate_user_id,
                                   validate_username, validate_password, validate_search_query,
                                   validate_post_content, validate_comment
  2. security/immutable_audit_log.py — additional edge cases (deletion tamper, genesis hash,
                                        target_id, detail_json round-trip)
  3. security/action_classifier.py   — additional destructive patterns (mkfs, dd, format,
                                        kill -9, reboot), SELECT INTO edge case
  4. security/dlp_engine.py          — selective scan_types, credit card with dashes,
                                        real IP detection, mixed PII text
  5. security/secrets_manager.py     — Fernet encrypt/decrypt round-trip with real keys
  6. security/safe_deserialize.py    — RestrictedUnpickler edge cases
  7. security/node_integrity.py      — compute_code_hash on real dir, file manifest,
                                        sign/verify JSON round-trip with fresh keys

Run: pytest tests/functional/test_security_modules_functional.py -v --noconftest
"""

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ═══════════════════════════════════════════════════════════════════════
# 1. security/sanitize.py — Untested validators
# ═══════════════════════════════════════════════════════════════════════

class TestEscapeLike(unittest.TestCase):
    """SQL LIKE wildcard escaping."""

    def test_percent_escaped(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like("100%"), "100\\%")

    def test_underscore_escaped(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like("user_name"), "user\\_name")

    def test_backslash_escaped(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like("C:\\path"), "C:\\\\path")

    def test_combined_escapes(self):
        from security.sanitize import escape_like
        result = escape_like("100% of_users\\all")
        self.assertEqual(result, "100\\% of\\_users\\\\all")

    def test_clean_string_unchanged(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like("hello world"), "hello world")

    def test_empty_string(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like(""), "")

    def test_all_wildcards(self):
        from security.sanitize import escape_like
        self.assertEqual(escape_like("%_%"), "\\%\\_\\%")


class TestSanitizePath(unittest.TestCase):
    """Path traversal prevention."""

    def test_valid_filename_accepted(self):
        from security.sanitize import sanitize_path
        with tempfile.TemporaryDirectory() as tmpdir:
            result = sanitize_path("test.json", tmpdir)
            self.assertTrue(result.endswith("test.json"))
            self.assertTrue(result.startswith(str(Path(tmpdir).resolve())))

    def test_dotdot_stripped(self):
        from security.sanitize import sanitize_path
        with tempfile.TemporaryDirectory() as tmpdir:
            # .. is stripped, so this stays within base_dir
            result = sanitize_path("../etc/passwd", tmpdir)
            self.assertTrue(str(Path(tmpdir).resolve()) in result)

    def test_forward_slash_stripped(self):
        from security.sanitize import sanitize_path
        with tempfile.TemporaryDirectory() as tmpdir:
            result = sanitize_path("sub/dir/file.txt", tmpdir)
            # slashes are stripped, so it becomes "subdirfile.txt"
            self.assertIn("subdirfile.txt", result)

    def test_backslash_stripped(self):
        from security.sanitize import sanitize_path
        with tempfile.TemporaryDirectory() as tmpdir:
            result = sanitize_path("sub\\dir\\file.txt", tmpdir)
            self.assertIn("subdirfile.txt", result)

    def test_simple_filename_resolves(self):
        from security.sanitize import sanitize_path
        with tempfile.TemporaryDirectory() as tmpdir:
            result = sanitize_path("12345.json", tmpdir)
            expected = str((Path(tmpdir).resolve() / "12345.json"))
            self.assertEqual(result, expected)


class TestSanitizeHtml(unittest.TestCase):
    """XSS prevention via HTML entity escaping."""

    def test_script_tag_escaped(self):
        from security.sanitize import sanitize_html
        result = sanitize_html("<script>alert('xss')</script>")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_quotes_escaped(self):
        from security.sanitize import sanitize_html
        result = sanitize_html('value="injected"')
        self.assertIn("&quot;", result)

    def test_ampersand_escaped(self):
        from security.sanitize import sanitize_html
        result = sanitize_html("A & B")
        self.assertIn("&amp;", result)

    def test_non_string_returns_unchanged(self):
        from security.sanitize import sanitize_html
        self.assertEqual(sanitize_html(42), 42)
        self.assertEqual(sanitize_html(None), None)

    def test_clean_text_unchanged(self):
        from security.sanitize import sanitize_html
        self.assertEqual(sanitize_html("hello world"), "hello world")

    def test_angle_brackets_escaped(self):
        from security.sanitize import sanitize_html
        result = sanitize_html("<img src=x onerror=alert(1)>")
        self.assertNotIn("<img", result)
        self.assertIn("&lt;img", result)


class TestValidateInput(unittest.TestCase):
    """Generic input validation."""

    def test_valid_input(self):
        from security.sanitize import validate_input
        result = validate_input("hello", max_length=100, min_length=1)
        self.assertEqual(result, "hello")

    def test_strips_whitespace(self):
        from security.sanitize import validate_input
        result = validate_input("  hello  ")
        self.assertEqual(result, "hello")

    def test_too_short_raises(self):
        from security.sanitize import validate_input
        with self.assertRaises(ValueError) as ctx:
            validate_input("ab", min_length=5)
        self.assertIn("at least 5", str(ctx.exception))

    def test_too_long_raises(self):
        from security.sanitize import validate_input
        with self.assertRaises(ValueError) as ctx:
            validate_input("x" * 200, max_length=100)
        self.assertIn("maximum length", str(ctx.exception))

    def test_pattern_match_passes(self):
        from security.sanitize import validate_input
        result = validate_input("abc123", pattern=r'^[a-z0-9]+$')
        self.assertEqual(result, "abc123")

    def test_pattern_mismatch_raises(self):
        from security.sanitize import validate_input
        with self.assertRaises(ValueError) as ctx:
            validate_input("abc@#$", pattern=r'^[a-z0-9]+$')
        self.assertIn("invalid characters", str(ctx.exception))

    def test_non_string_raises(self):
        from security.sanitize import validate_input
        with self.assertRaises(ValueError) as ctx:
            validate_input(12345)
        self.assertIn("must be a string", str(ctx.exception))

    def test_custom_field_name_in_error(self):
        from security.sanitize import validate_input
        with self.assertRaises(ValueError) as ctx:
            validate_input("", min_length=1, field_name='email')
        self.assertIn("email", str(ctx.exception))

    def test_empty_string_within_bounds(self):
        from security.sanitize import validate_input
        result = validate_input("  ", min_length=0, max_length=100)
        self.assertEqual(result, "")


class TestValidatePromptId(unittest.TestCase):
    """Prompt ID validation (numeric only)."""

    def test_valid_numeric_string(self):
        from security.sanitize import validate_prompt_id
        self.assertEqual(validate_prompt_id("12345"), "12345")

    def test_integer_input_converted(self):
        from security.sanitize import validate_prompt_id
        self.assertEqual(validate_prompt_id(12345), "12345")

    def test_non_numeric_raises(self):
        from security.sanitize import validate_prompt_id
        with self.assertRaises(ValueError) as ctx:
            validate_prompt_id("abc")
        self.assertIn("numeric", str(ctx.exception))

    def test_mixed_alphanumeric_raises(self):
        from security.sanitize import validate_prompt_id
        with self.assertRaises(ValueError):
            validate_prompt_id("123abc")

    def test_negative_number_raises(self):
        from security.sanitize import validate_prompt_id
        with self.assertRaises(ValueError):
            validate_prompt_id("-1")

    def test_whitespace_stripped(self):
        from security.sanitize import validate_prompt_id
        self.assertEqual(validate_prompt_id("  42  "), "42")

    def test_sql_injection_blocked(self):
        from security.sanitize import validate_prompt_id
        with self.assertRaises(ValueError):
            validate_prompt_id("1; DROP TABLE users")


class TestValidateUserId(unittest.TestCase):
    """User ID validation (alphanumeric + underscore + hyphen)."""

    def test_valid_alphanumeric(self):
        from security.sanitize import validate_user_id
        self.assertEqual(validate_user_id("user123"), "user123")

    def test_underscore_allowed(self):
        from security.sanitize import validate_user_id
        self.assertEqual(validate_user_id("user_name"), "user_name")

    def test_hyphen_allowed(self):
        from security.sanitize import validate_user_id
        self.assertEqual(validate_user_id("user-name"), "user-name")

    def test_special_chars_rejected(self):
        from security.sanitize import validate_user_id
        with self.assertRaises(ValueError):
            validate_user_id("user@name")

    def test_spaces_rejected(self):
        from security.sanitize import validate_user_id
        with self.assertRaises(ValueError):
            validate_user_id("user name")

    def test_integer_input_converted(self):
        from security.sanitize import validate_user_id
        self.assertEqual(validate_user_id(999), "999")

    def test_whitespace_stripped(self):
        from security.sanitize import validate_user_id
        self.assertEqual(validate_user_id("  user1  "), "user1")

    def test_sql_injection_blocked(self):
        from security.sanitize import validate_user_id
        with self.assertRaises(ValueError):
            validate_user_id("1' OR '1'='1")

    def test_path_traversal_blocked(self):
        from security.sanitize import validate_user_id
        with self.assertRaises(ValueError):
            validate_user_id("../../etc/passwd")


class TestValidateUsername(unittest.TestCase):
    """Social platform username validation."""

    def test_valid_username(self):
        from security.sanitize import validate_username
        self.assertEqual(validate_username("john_doe"), "john_doe")

    def test_email_like_username(self):
        from security.sanitize import validate_username
        self.assertEqual(validate_username("user@domain"), "user@domain")

    def test_too_short_rejected(self):
        from security.sanitize import validate_username
        with self.assertRaises(ValueError) as ctx:
            validate_username("a")
        self.assertIn("at least 2", str(ctx.exception))

    def test_too_long_rejected(self):
        from security.sanitize import validate_username
        with self.assertRaises(ValueError):
            validate_username("a" * 51)

    def test_max_length_accepted(self):
        from security.sanitize import validate_username
        result = validate_username("a" * 50)
        self.assertEqual(len(result), 50)

    def test_special_chars_rejected(self):
        from security.sanitize import validate_username
        with self.assertRaises(ValueError):
            validate_username("user name!")  # space and ! not allowed


class TestValidatePassword(unittest.TestCase):
    """Password validation."""

    def test_valid_password(self):
        from security.sanitize import validate_password
        result = validate_password("StrongP@ss1")
        self.assertEqual(result, "StrongP@ss1")

    def test_too_short_rejected(self):
        from security.sanitize import validate_password
        with self.assertRaises(ValueError) as ctx:
            validate_password("short")
        self.assertIn("at least 8", str(ctx.exception))

    def test_too_long_rejected(self):
        from security.sanitize import validate_password
        with self.assertRaises(ValueError):
            validate_password("a" * 129)

    def test_exact_min_length_accepted(self):
        from security.sanitize import validate_password
        result = validate_password("12345678")
        self.assertEqual(result, "12345678")

    def test_exact_max_length_accepted(self):
        from security.sanitize import validate_password
        result = validate_password("a" * 128)
        self.assertEqual(len(result), 128)


class TestValidateSearchQuery(unittest.TestCase):
    """Search query validation."""

    def test_valid_query(self):
        from security.sanitize import validate_search_query
        self.assertEqual(validate_search_query("python tutorials"), "python tutorials")

    def test_empty_rejected(self):
        from security.sanitize import validate_search_query
        with self.assertRaises(ValueError):
            validate_search_query("")

    def test_too_long_rejected(self):
        from security.sanitize import validate_search_query
        with self.assertRaises(ValueError):
            validate_search_query("x" * 201)


class TestValidatePostContent(unittest.TestCase):
    """Post content validation."""

    def test_valid_content(self):
        from security.sanitize import validate_post_content
        self.assertEqual(validate_post_content("Hello world"), "Hello world")

    def test_empty_rejected(self):
        from security.sanitize import validate_post_content
        with self.assertRaises(ValueError):
            validate_post_content("")

    def test_max_length_boundary(self):
        from security.sanitize import validate_post_content
        # 40000 chars should pass
        result = validate_post_content("a" * 40000)
        self.assertEqual(len(result), 40000)

    def test_over_max_rejected(self):
        from security.sanitize import validate_post_content
        with self.assertRaises(ValueError):
            validate_post_content("a" * 40001)


class TestValidateComment(unittest.TestCase):
    """Comment content validation."""

    def test_valid_comment(self):
        from security.sanitize import validate_comment
        self.assertEqual(validate_comment("Great post!"), "Great post!")

    def test_empty_rejected(self):
        from security.sanitize import validate_comment
        with self.assertRaises(ValueError):
            validate_comment("")

    def test_max_length_boundary(self):
        from security.sanitize import validate_comment
        result = validate_comment("a" * 10000)
        self.assertEqual(len(result), 10000)

    def test_over_max_rejected(self):
        from security.sanitize import validate_comment
        with self.assertRaises(ValueError):
            validate_comment("a" * 10001)


# ═══════════════════════════════════════════════════════════════════════
# 2. security/immutable_audit_log.py — Additional edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestAuditLogGenesisHash(unittest.TestCase):
    """Verify the hash chain starts from 'genesis'."""

    def test_first_entry_chains_from_genesis(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        log.log_event('auth', actor_id='user_1', action='login')
        self.assertEqual(log._memory_log[0]['prev_hash'], 'genesis')

    def test_second_entry_chains_from_first(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        _, first_hash = log.log_event('auth', actor_id='user_1', action='login')
        log.log_event('auth', actor_id='user_1', action='logout')
        self.assertEqual(log._memory_log[1]['prev_hash'], first_hash)


class TestAuditLogDeletionTamper(unittest.TestCase):
    """Deleting an entry from the middle should break the chain."""

    def test_deletion_breaks_chain(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        for i in range(5):
            log.log_event('test', actor_id='user_1', action=f'action_{i}')

        ok, reason = log.verify_chain()
        self.assertTrue(ok)

        # Delete middle entry
        del log._memory_log[2]

        ok, reason = log.verify_chain()
        self.assertFalse(ok)
        self.assertIn('Chain broken', reason)


class TestAuditLogTargetId(unittest.TestCase):
    """Target ID tracking in audit entries."""

    def test_target_id_stored(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        log.log_event('state_change', actor_id='user_1', action='approved',
                       target_id='task_42')
        self.assertEqual(log._memory_log[0]['target_id'], 'task_42')

    def test_target_id_none_by_default(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        log.log_event('test', actor_id='user_1', action='test')
        self.assertIsNone(log._memory_log[0]['target_id'])


class TestAuditLogDetailJsonRoundTrip(unittest.TestCase):
    """Structured detail data is preserved through JSON serialization."""

    def test_detail_json_preserved(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        detail = {'user_agent': 'Mozilla/5.0', 'ip': '8.8.8.8', 'method': 'POST'}
        log.log_event('auth', actor_id='user_1', action='login', detail=detail)

        stored = json.loads(log._memory_log[0]['detail_json'])
        self.assertEqual(stored['user_agent'], 'Mozilla/5.0')
        self.assertEqual(stored['ip'], '8.8.8.8')

    def test_multiple_sensitive_keys_redacted(self):
        from security.immutable_audit_log import _redact_sensitive
        detail = {
            'username': 'alice',
            'password': 'hunter2',
            'api_key': 'sk-123',
            'token': 'jwt.payload.sig',
            'action': 'login',
        }
        result = json.loads(_redact_sensitive(detail))
        self.assertEqual(result['username'], 'alice')
        self.assertEqual(result['action'], 'login')
        self.assertEqual(result['password'], '[REDACTED]')
        self.assertEqual(result['api_key'], '[REDACTED]')
        self.assertEqual(result['token'], '[REDACTED]')

    def test_redact_nested_key_names(self):
        """Keys containing sensitive substrings are also caught."""
        from security.immutable_audit_log import _redact_sensitive
        detail = {
            'old_password': 'abc',
            'new_credential': 'xyz',
            'private_key_path': '/keys/node.pem',
        }
        result = json.loads(_redact_sensitive(detail))
        self.assertEqual(result['old_password'], '[REDACTED]')
        self.assertEqual(result['new_credential'], '[REDACTED]')
        self.assertEqual(result['private_key_path'], '[REDACTED]')


class TestAuditLogLargeChain(unittest.TestCase):
    """Chain verification on a large number of entries."""

    def test_100_entries_chain_valid(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        for i in range(100):
            log.log_event('batch', actor_id=f'worker_{i % 10}', action=f'step_{i}')

        ok, reason = log.verify_chain()
        self.assertTrue(ok)
        self.assertIn('100 entries', reason)

    def test_tamper_last_entry_detected(self):
        from security.immutable_audit_log import ImmutableAuditLog
        log = ImmutableAuditLog()
        log._use_db = False
        log._memory_log = []

        for i in range(50):
            log.log_event('test', actor_id='user_1', action=f'action_{i}')

        # Tamper with last entry's hash
        log._memory_log[-1]['entry_hash'] = 'deadbeef' * 8

        ok, reason = log.verify_chain()
        self.assertFalse(ok)


# ═══════════════════════════════════════════════════════════════════════
# 3. security/action_classifier.py — Additional pattern coverage
# ═══════════════════════════════════════════════════════════════════════

class TestActionClassifierAdditionalPatterns(unittest.TestCase):
    """Patterns not covered by existing unit tests."""

    def test_mkfs_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("mkfs.ext4 /dev/sda1"), 'destructive')

    def test_dd_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("dd if=/dev/zero of=/dev/sda"), 'destructive')

    def test_format_drive_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("format C: /FS:NTFS"), 'destructive')

    def test_kill_9_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("kill -9 12345"), 'destructive')

    def test_reboot_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("reboot now"), 'destructive')

    def test_git_clean_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("git clean -fd"), 'destructive')

    def test_overwrite_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("overwrite the production config"), 'destructive')

    def test_purge_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("purge all cached data"), 'destructive')

    def test_wipe_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("wipe the disk clean"), 'destructive')

    def test_erase_is_destructive(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("erase all user data"), 'destructive')

    def test_cat_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("cat /etc/hostname"), 'safe')

    def test_ls_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("ls -la /tmp"), 'safe')

    def test_git_log_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("git log --oneline"), 'safe')

    def test_git_diff_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("git diff HEAD~1"), 'safe')

    def test_describe_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("describe the table schema"), 'safe')

    def test_explain_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("explain this error message"), 'safe')

    def test_fetch_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("fetch the latest data"), 'safe')

    def test_view_is_safe(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action("view the report"), 'safe')

    def test_none_input_returns_unknown(self):
        from security.action_classifier import classify_action
        self.assertEqual(classify_action(None), 'unknown')


class TestShouldPreviewEdgeCases(unittest.TestCase):
    """Edge cases for the preview gate."""

    def test_preview_disabled_destructive_still_false(self):
        from security.action_classifier import should_preview
        # Even a destructive action returns False when preview is disabled
        self.assertFalse(should_preview("rm -rf /", preview_enabled=False))

    def test_preview_enabled_empty_input_returns_true(self):
        from security.action_classifier import should_preview
        # Empty input is 'unknown', which should be previewed
        self.assertTrue(should_preview("", preview_enabled=True))

    def test_preview_enabled_safe_returns_false(self):
        from security.action_classifier import should_preview
        self.assertFalse(should_preview("SELECT * FROM users", preview_enabled=True))


# ═══════════════════════════════════════════════════════════════════════
# 4. security/dlp_engine.py — Additional edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestDLPSelectiveScanTypes(unittest.TestCase):
    """Scan only specific PII types when scan_types is set."""

    def test_email_only_scan(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['email'])
        text = "john@example.com, SSN 123-45-6789"
        findings = dlp.scan(text)
        types = [f[0] for f in findings]
        self.assertIn('email', types)
        self.assertNotIn('ssn', types)

    def test_ssn_only_scan(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['ssn'])
        text = "john@example.com, SSN 123-45-6789"
        findings = dlp.scan(text)
        types = [f[0] for f in findings]
        self.assertNotIn('email', types)
        self.assertIn('ssn', types)


class TestDLPCreditCardFormats(unittest.TestCase):
    """Credit card number detection with various separators."""

    def test_spaces_separated(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['credit_card'])
        findings = dlp.scan("Card: 4111 1111 1111 1111")
        self.assertTrue(any(f[0] == 'credit_card' for f in findings))

    def test_dash_separated(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['credit_card'])
        findings = dlp.scan("Card: 4111-1111-1111-1111")
        self.assertTrue(any(f[0] == 'credit_card' for f in findings))

    def test_no_separator(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['credit_card'])
        findings = dlp.scan("Card: 4111111111111111")
        self.assertTrue(any(f[0] == 'credit_card' for f in findings))


class TestDLPIPDetection(unittest.TestCase):
    """IP address detection with safe-list filtering."""

    def test_public_ip_detected(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['ip_address'])
        findings = dlp.scan("Server at 203.0.113.42")
        self.assertTrue(any(f[0] == 'ip_address' for f in findings))

    def test_safe_ips_not_flagged(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['ip_address'])
        for safe_ip in ['127.0.0.1', '0.0.0.0', '255.255.255.255']:
            findings = dlp.scan(f"Connecting to {safe_ip}")
            ip_findings = [f for f in findings if f[0] == 'ip_address']
            self.assertEqual(len(ip_findings), 0,
                             f"{safe_ip} should be in safe list")


class TestDLPRedactSpecificTypes(unittest.TestCase):
    """Verify each PII type gets its own redaction placeholder."""

    def test_redact_phone(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        result = dlp.redact("Call 555-123-4567")
        self.assertIn('[PHONE_REDACTED]', result)

    def test_redact_credit_card(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        result = dlp.redact("Card: 4111 1111 1111 1111")
        self.assertIn('[CC_REDACTED]', result)

    def test_redact_ip(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine()
        result = dlp.redact("Server at 203.0.113.42")
        self.assertIn('[IP_REDACTED]', result)


class TestDLPMultipleFindings(unittest.TestCase):
    """Multiple PII items of same and different types."""

    def test_multiple_emails(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(scan_types=['email'])
        text = "Contact alice@test.com or bob@test.com"
        findings = dlp.scan(text)
        emails = [f[1] for f in findings if f[0] == 'email']
        self.assertEqual(len(emails), 2)

    def test_check_outbound_reports_all_types(self):
        from security.dlp_engine import DLPEngine
        dlp = DLPEngine(block_on_pii=True)
        text = "Email alice@test.com, SSN 123-45-6789, call 555-123-4567"
        allowed, reason = dlp.check_outbound(text)
        self.assertFalse(allowed)
        self.assertIn('email', reason)
        self.assertIn('ssn', reason)
        self.assertIn('phone', reason)


# ═══════════════════════════════════════════════════════════════════════
# 5. security/secrets_manager.py — Real Fernet round-trip
# ═══════════════════════════════════════════════════════════════════════

class TestSecretsManagerRealFernet(unittest.TestCase):
    """Fernet encrypt/decrypt round-trip using real cryptography."""

    def setUp(self):
        from security.secrets_manager import SecretsManager
        SecretsManager.reset()

    def tearDown(self):
        from security.secrets_manager import SecretsManager
        SecretsManager.reset()

    def test_set_and_get_multiple_secrets(self):
        """Store multiple secrets and retrieve them all after reload."""
        from security.secrets_manager import SecretsManager
        with tempfile.TemporaryDirectory() as tmpdir:
            salt_path = os.path.join(tmpdir, 'secrets.salt')
            vault_path = os.path.join(tmpdir, 'secrets.enc')

            with patch('security.secrets_manager._SALT_PATH', salt_path), \
                 patch('security.secrets_manager._VAULT_PATH', vault_path), \
                 patch.dict(os.environ, {'HEVOLVE_MASTER_KEY': 'test-master-key-functional-suite'}):
                SecretsManager.reset()
                sm = SecretsManager.get_instance()
                sm.set_secret('KEY_A', 'value_a')
                sm.set_secret('KEY_B', 'value_b')
                sm.set_secret('KEY_C', 'value_c')

                # Reload from disk
                SecretsManager.reset()
                sm2 = SecretsManager.get_instance()

                # Clear env to ensure we read from vault
                env_clean = {k: v for k, v in os.environ.items()
                             if k not in ('KEY_A', 'KEY_B', 'KEY_C')}
                with patch.dict(os.environ, env_clean, clear=True):
                    self.assertEqual(sm2.get_secret('KEY_A'), 'value_a')
                    self.assertEqual(sm2.get_secret('KEY_B'), 'value_b')
                    self.assertEqual(sm2.get_secret('KEY_C'), 'value_c')

    def test_vault_file_is_encrypted_on_disk(self):
        """The vault file should not contain plaintext secrets."""
        from security.secrets_manager import SecretsManager
        with tempfile.TemporaryDirectory() as tmpdir:
            salt_path = os.path.join(tmpdir, 'secrets.salt')
            vault_path = os.path.join(tmpdir, 'secrets.enc')

            with patch('security.secrets_manager._SALT_PATH', salt_path), \
                 patch('security.secrets_manager._VAULT_PATH', vault_path), \
                 patch.dict(os.environ, {'HEVOLVE_MASTER_KEY': 'test-master-key-functional-suite'}):
                SecretsManager.reset()
                sm = SecretsManager.get_instance()
                sm.set_secret('SENSITIVE_DATA', 'super_secret_value_12345')

                # Read raw file and verify it does NOT contain plaintext
                with open(vault_path, 'rb') as f:
                    raw = f.read()
                self.assertNotIn(b'super_secret_value_12345', raw)
                self.assertNotIn(b'SENSITIVE_DATA', raw)

    def test_salt_file_created(self):
        """Salt file should be created on first init."""
        from security.secrets_manager import SecretsManager
        with tempfile.TemporaryDirectory() as tmpdir:
            salt_path = os.path.join(tmpdir, 'secrets.salt')
            vault_path = os.path.join(tmpdir, 'secrets.enc')

            self.assertFalse(os.path.exists(salt_path))

            with patch('security.secrets_manager._SALT_PATH', salt_path), \
                 patch('security.secrets_manager._VAULT_PATH', vault_path), \
                 patch.dict(os.environ, {'HEVOLVE_MASTER_KEY': 'test-master-key-functional-suite'}):
                SecretsManager.reset()
                SecretsManager.get_instance()

            self.assertTrue(os.path.exists(salt_path))
            with open(salt_path, 'rb') as f:
                salt = f.read()
            self.assertEqual(len(salt), 16)

    def test_secret_keys_list_has_expected_entries(self):
        """SECRET_KEYS constant should list all known API keys."""
        from security.secrets_manager import SECRET_KEYS
        self.assertIn('OPENAI_API_KEY', SECRET_KEYS)
        self.assertIn('GROQ_API_KEY', SECRET_KEYS)
        self.assertIn('LANGCHAIN_API_KEY', SECRET_KEYS)


# ═══════════════════════════════════════════════════════════════════════
# 6. security/safe_deserialize.py — Additional edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestSafeDeserializeEdgeCases(unittest.TestCase):
    """Edge cases not covered by existing tests."""

    def test_1d_array_roundtrip(self):
        """1-dimensional array serialization."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not available")
        from security.safe_deserialize import safe_dump_frame, safe_load_frame
        arr = np.array([1, 2, 3, 4, 5], dtype=np.int32)
        data = safe_dump_frame(arr)
        loaded = safe_load_frame(data)
        self.assertTrue(np.array_equal(arr, loaded))

    def test_empty_array_roundtrip(self):
        """Empty array edge case."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not available")
        from security.safe_deserialize import safe_dump_frame, safe_load_frame
        arr = np.array([], dtype=np.float64)
        data = safe_dump_frame(arr)
        loaded = safe_load_frame(data)
        self.assertEqual(loaded.shape, (0,))

    def test_large_array_roundtrip(self):
        """Large array to test chunked processing."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not available")
        from security.safe_deserialize import safe_dump_frame, safe_load_frame
        arr = np.random.randint(0, 255, size=(1080, 1920, 3), dtype=np.uint8)
        data = safe_dump_frame(arr)
        loaded = safe_load_frame(data)
        self.assertTrue(np.array_equal(arr, loaded))

    def test_restricted_unpickler_blocks_io_module(self):
        """io module should be blocked by RestrictedUnpickler."""
        from security.safe_deserialize import safe_load_frame
        malicious = b"cio\nopen\n(S'/etc/passwd'\ntR."
        result = safe_load_frame(malicious)
        self.assertIsNone(result)

    def test_restricted_unpickler_blocks_importlib(self):
        """importlib should be blocked."""
        from security.safe_deserialize import safe_load_frame
        malicious = b"cimportlib\nimport_module\n(S'os'\ntR."
        result = safe_load_frame(malicious)
        self.assertIsNone(result)

    def test_magic_bytes_constant(self):
        from security.safe_deserialize import _MAGIC
        self.assertEqual(_MAGIC, b'HVSF')

    def test_corrupted_header_size_handled(self):
        """Corrupted header size should raise an error, not crash silently."""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not available")
        from security.safe_deserialize import _MAGIC
        # Valid magic but absurdly large header size pointing past data
        data = _MAGIC + struct.pack('<I', 999999) + b'\x00' * 10
        from security.safe_deserialize import safe_load_frame
        with self.assertRaises(Exception):
            safe_load_frame(data)


# ═══════════════════════════════════════════════════════════════════════
# 7. security/node_integrity.py — Crypto operations with real keys
# ═══════════════════════════════════════════════════════════════════════

class TestNodeIntegritySignVerify(unittest.TestCase):
    """Ed25519 sign/verify round-trip with fresh keys."""

    def setUp(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_sign_and_verify_message(self):
        """Sign a message and verify it with the public key."""
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                sign_message, verify_signature, get_public_key_hex,
                reset_keypair,
            )
            reset_keypair()
            msg = b"test message for signing"
            sig = sign_message(msg)
            pub_hex = get_public_key_hex()

            self.assertTrue(verify_signature(pub_hex, msg, sig))

    def test_verify_fails_with_wrong_key(self):
        """Verification must fail when a different key is used."""
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import sign_message, verify_signature, reset_keypair
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            reset_keypair()
            msg = b"test message"
            sig = sign_message(msg)

            # Generate a different key
            other_key = Ed25519PrivateKey.generate()
            from cryptography.hazmat.primitives import serialization
            other_pub_hex = other_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ).hex()

            self.assertFalse(verify_signature(other_pub_hex, msg, sig))

    def test_verify_fails_with_tampered_message(self):
        """Verification must fail when the message is tampered."""
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                sign_message, verify_signature, get_public_key_hex,
                reset_keypair,
            )
            reset_keypair()
            msg = b"original message"
            sig = sign_message(msg)
            pub_hex = get_public_key_hex()

            self.assertFalse(verify_signature(pub_hex, b"tampered message", sig))


class TestNodeIntegrityJsonSignVerify(unittest.TestCase):
    """JSON payload sign/verify round-trip."""

    def setUp(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        from security.node_integrity import reset_keypair
        reset_keypair()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_json_sign_verify_roundtrip(self):
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                sign_json_payload, verify_json_signature,
                get_public_key_hex, reset_keypair,
            )
            reset_keypair()
            payload = {'action': 'deploy', 'version': '1.2.3', 'node_id': 'abc'}
            sig_hex = sign_json_payload(payload)
            pub_hex = get_public_key_hex()

            self.assertTrue(verify_json_signature(pub_hex, payload, sig_hex))

    def test_json_verify_fails_on_tampered_payload(self):
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                sign_json_payload, verify_json_signature,
                get_public_key_hex, reset_keypair,
            )
            reset_keypair()
            payload = {'action': 'deploy', 'version': '1.2.3'}
            sig_hex = sign_json_payload(payload)
            pub_hex = get_public_key_hex()

            tampered = {'action': 'deploy', 'version': '9.9.9'}
            self.assertFalse(verify_json_signature(pub_hex, tampered, sig_hex))

    def test_signature_key_stripped_from_payload(self):
        """The 'signature' key should be stripped before signing."""
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                sign_json_payload, verify_json_signature,
                get_public_key_hex, reset_keypair,
            )
            reset_keypair()
            payload = {'action': 'deploy', 'version': '1.2.3'}
            sig_hex = sign_json_payload(payload)
            pub_hex = get_public_key_hex()

            # Add signature key to payload - should still verify
            payload_with_sig = {**payload, 'signature': sig_hex}
            self.assertTrue(verify_json_signature(pub_hex, payload_with_sig, sig_hex))

    def test_json_verify_fails_on_invalid_hex(self):
        with patch.dict(os.environ, {'HEVOLVE_KEY_DIR': self._tmpdir}), \
             patch('security.node_integrity._KEY_DIR', self._tmpdir):
            from security.node_integrity import (
                verify_json_signature, get_public_key_hex, reset_keypair,
            )
            reset_keypair()
            pub_hex = get_public_key_hex()
            self.assertFalse(
                verify_json_signature(pub_hex, {'a': 1}, 'not-valid-hex')
            )


class TestComputeCodeHash(unittest.TestCase):
    """compute_code_hash on a controlled directory."""

    def test_hash_deterministic_on_same_files(self):
        from security.node_integrity import compute_code_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some .py files
            (Path(tmpdir) / 'a.py').write_text('print("hello")')
            (Path(tmpdir) / 'b.py').write_text('x = 1')
            subdir = Path(tmpdir) / 'sub'
            subdir.mkdir()
            (subdir / 'c.py').write_text('import os')

            h1 = compute_code_hash(tmpdir)
            h2 = compute_code_hash(tmpdir)
            self.assertEqual(h1, h2)

    def test_hash_changes_on_file_modification(self):
        from security.node_integrity import compute_code_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / 'module.py'
            py_file.write_text('x = 1')

            h1 = compute_code_hash(tmpdir)

            py_file.write_text('x = 2')

            # Delete the code hash cache so it recomputes
            cache_file = Path(tmpdir) / 'agent_data' / 'code_hash_cache.json'
            if cache_file.exists():
                cache_file.unlink()

            h2 = compute_code_hash(tmpdir)
            self.assertNotEqual(h1, h2)

    def test_hash_changes_on_file_addition(self):
        from security.node_integrity import compute_code_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'a.py').write_text('x = 1')
            h1 = compute_code_hash(tmpdir)

            (Path(tmpdir) / 'b.py').write_text('y = 2')

            # Delete the code hash cache so it recomputes
            cache_file = Path(tmpdir) / 'agent_data' / 'code_hash_cache.json'
            if cache_file.exists():
                cache_file.unlink()

            h2 = compute_code_hash(tmpdir)
            self.assertNotEqual(h1, h2)

    def test_excludes_pycache(self):
        """__pycache__ directories should be excluded from hash."""
        from security.node_integrity import compute_code_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'module.py').write_text('x = 1')
            h1 = compute_code_hash(tmpdir)

            # Add a .py file inside __pycache__ -- should not change hash
            pycache = Path(tmpdir) / '__pycache__'
            pycache.mkdir()
            (pycache / 'module.cpython-310.pyc').write_text('cached')
            h2 = compute_code_hash(tmpdir)
            self.assertEqual(h1, h2)

    def test_precomputed_hash_env_override(self):
        """HEVOLVE_CODE_HASH_PRECOMPUTED env var should bypass computation."""
        from security.node_integrity import compute_code_hash
        with patch.dict(os.environ, {'HEVOLVE_CODE_HASH_PRECOMPUTED': 'abc123precomputed'}):
            result = compute_code_hash('/nonexistent/path')
            self.assertEqual(result, 'abc123precomputed')

    def test_empty_dir_produces_valid_hash(self):
        from security.node_integrity import compute_code_hash
        with tempfile.TemporaryDirectory() as tmpdir:
            h = compute_code_hash(tmpdir)
            self.assertEqual(len(h), 64)  # SHA-256 hex


class TestComputeFileManifest(unittest.TestCase):
    """File manifest generation."""

    def test_manifest_contains_all_py_files(self):
        from security.node_integrity import compute_file_manifest
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'a.py').write_text('print("a")')
            (Path(tmpdir) / 'b.py').write_text('print("b")')
            (Path(tmpdir) / 'readme.txt').write_text('not python')
            sub = Path(tmpdir) / 'pkg'
            sub.mkdir()
            (sub / 'c.py').write_text('print("c")')

            manifest = compute_file_manifest(tmpdir)
            self.assertIn('a.py', manifest)
            self.assertIn('b.py', manifest)
            self.assertIn('pkg/c.py', manifest)
            self.assertNotIn('readme.txt', manifest)

    def test_manifest_hashes_are_sha256(self):
        from security.node_integrity import compute_file_manifest
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'test.py').write_text('pass')
            manifest = compute_file_manifest(tmpdir)
            for _, file_hash in manifest.items():
                self.assertEqual(len(file_hash), 64)

    def test_manifest_excludes_venv(self):
        from security.node_integrity import compute_file_manifest
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / 'app.py').write_text('x=1')
            venv = Path(tmpdir) / 'venv310'
            venv.mkdir()
            (venv / 'site.py').write_text('venv_code')

            manifest = compute_file_manifest(tmpdir)
            self.assertIn('app.py', manifest)
            # venv310 is in _EXCLUDE_DIRS
            venv_keys = [k for k in manifest if 'venv310' in k]
            self.assertEqual(len(venv_keys), 0)


class TestHashFile(unittest.TestCase):
    """Single file hashing."""

    def test_hash_known_content(self):
        from security.node_integrity import _hash_file
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / 'test.py'
            fpath.write_bytes(b'hello world')
            result = _hash_file(fpath)
            expected = hashlib.sha256(b'hello world').hexdigest()
            self.assertEqual(result, expected)

    def test_hash_empty_file(self):
        from security.node_integrity import _hash_file
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / 'empty.py'
            fpath.write_bytes(b'')
            result = _hash_file(fpath)
            expected = hashlib.sha256(b'').hexdigest()
            self.assertEqual(result, expected)


class TestPurgePycache(unittest.TestCase):
    """__pycache__ purge functionality."""

    def test_purge_removes_pycache_dirs(self):
        from security.node_integrity import purge_pycache
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some __pycache__ directories
            (Path(tmpdir) / '__pycache__').mkdir()
            sub = Path(tmpdir) / 'subpkg'
            sub.mkdir()
            (sub / '__pycache__').mkdir()

            count = purge_pycache(tmpdir)
            self.assertEqual(count, 2)
            self.assertFalse((Path(tmpdir) / '__pycache__').exists())
            self.assertFalse((sub / '__pycache__').exists())

    def test_purge_sets_env_var(self):
        from security.node_integrity import purge_pycache
        with tempfile.TemporaryDirectory() as tmpdir:
            # Remove env var if set
            old = os.environ.pop('PYTHONDONTWRITEBYTECODE', None)
            try:
                purge_pycache(tmpdir)
                self.assertEqual(os.environ.get('PYTHONDONTWRITEBYTECODE'), '1')
            finally:
                if old is not None:
                    os.environ['PYTHONDONTWRITEBYTECODE'] = old

    def test_purge_returns_zero_when_no_pycache(self):
        from security.node_integrity import purge_pycache
        with tempfile.TemporaryDirectory() as tmpdir:
            count = purge_pycache(tmpdir)
            self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
