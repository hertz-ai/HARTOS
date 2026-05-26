"""Tests for integrations.vlm.safety — Phase 6 of the VLM plan §5.

Three guard layers:
  - SessionGuard: per-session action cap + per-second throttle
  - is_window_blocked: process-name + title-pattern blocklist
  - AuditLogger: JSONL audit trail

Plus integration with execute_action(safety=True).
"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from integrations.vlm.safety import (
    SessionGuard, SafetyConfig, AuditLogger,
    is_window_blocked,
    DEFAULT_BLOCKED_PROCESSES, DEFAULT_BLOCKED_TITLE_PATTERNS,
    get_session_guard, get_audit_logger, reset_session_guard,
)


class TestSessionGuard(unittest.TestCase):
    """Per-session cap + per-second throttle."""

    def test_under_limit_returns_none(self):
        cfg = SafetyConfig(max_actions_per_session=5, max_actions_per_second=10)
        guard = SessionGuard(cfg)
        self.assertIsNone(guard.check())

    def test_session_cap_blocks_at_limit(self):
        cfg = SafetyConfig(max_actions_per_session=3, max_actions_per_second=100)
        guard = SessionGuard(cfg)
        for _ in range(3):
            guard.record()
        reason = guard.check()
        self.assertIsNotNone(reason)
        self.assertIn('session-cap', reason)

    def test_throttle_blocks_above_per_second_limit(self):
        cfg = SafetyConfig(max_actions_per_session=1000, max_actions_per_second=2)
        guard = SessionGuard(cfg)
        # Record 2 actions back-to-back → 3rd check should block.
        guard.record()
        guard.record()
        reason = guard.check()
        self.assertIsNotNone(reason)
        self.assertIn('throttle', reason)

    def test_throttle_clears_after_window(self):
        cfg = SafetyConfig(max_actions_per_session=1000, max_actions_per_second=2)
        guard = SessionGuard(cfg)
        guard.record()
        guard.record()
        # Manually shift recorded times back > 1s so window clears.
        guard.recent_action_times = type(guard.recent_action_times)(
            [t - 2.0 for t in guard.recent_action_times],
            maxlen=guard.recent_action_times.maxlen)
        self.assertIsNone(guard.check())

    def test_reset_clears_counters(self):
        cfg = SafetyConfig(max_actions_per_session=1)
        guard = SessionGuard(cfg)
        guard.record()
        self.assertIsNotNone(guard.check())  # blocked
        guard.reset()
        self.assertIsNone(guard.check())     # freed


class TestWindowBlocklist(unittest.TestCase):
    """is_window_blocked: process-name AND title-pattern checks."""

    def test_no_window_meta_passes(self):
        self.assertIsNone(is_window_blocked(None))
        self.assertIsNone(is_window_blocked({}))

    def test_blocked_process_lsass(self):
        meta = {'process_name': 'lsass.exe', 'title': 'whatever'}
        reason = is_window_blocked(meta)
        self.assertIsNotNone(reason)
        self.assertIn('process_blocked', reason)

    def test_blocked_process_full_path(self):
        """Process names sometimes come with full path on Win."""
        meta = {'process_name': 'C:\\Windows\\System32\\lsass.exe',
                'title': 'x'}
        reason = is_window_blocked(meta)
        self.assertIsNotNone(reason)

    def test_blocked_password_manager(self):
        meta = {'process_name': 'bitwarden.exe', 'title': 'My Vault'}
        reason = is_window_blocked(meta)
        self.assertIsNotNone(reason)

    def test_unblocked_normal_process(self):
        meta = {'process_name': 'notepad.exe', 'title': 'Untitled'}
        self.assertIsNone(is_window_blocked(meta))

    def test_blocked_banking_title(self):
        meta = {'process_name': 'chrome.exe',
                'title': 'Welcome to Online Banking - Chase'}
        reason = is_window_blocked(meta)
        self.assertIsNotNone(reason)
        self.assertIn('title_pattern', reason)

    def test_blocked_password_change_title(self):
        meta = {'process_name': 'chrome.exe',
                'title': 'Reset Password - GitHub'}
        reason = is_window_blocked(meta)
        self.assertIsNotNone(reason)

    def test_unblocked_normal_title(self):
        meta = {'process_name': 'chrome.exe',
                'title': 'GitHub Pull Request #123'}
        self.assertIsNone(is_window_blocked(meta))

    def test_custom_blocklist_extends_defaults(self):
        cfg = SafetyConfig(blocked_processes=('myapp.exe',))
        meta = {'process_name': 'myapp.exe', 'title': 'x'}
        self.assertIsNotNone(is_window_blocked(meta, cfg))
        # Custom blocklist REPLACES defaults; lsass no longer blocked
        # when the admin sets a custom list.  This is intentional —
        # admins who customize get full control.
        meta2 = {'process_name': 'lsass.exe', 'title': 'x'}
        self.assertIsNone(is_window_blocked(meta2, cfg))


class TestAuditLogger(unittest.TestCase):
    """JSONL append + screenshot hash + window metadata."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = SafetyConfig(audit_dir=self.tmpdir, audit_enabled=True)
        self.logger = AuditLogger(self.cfg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_creates_jsonl_file(self):
        self.logger.log(
            {'action': 'left_click', 'coordinate': [100, 200]},
            {'output': 'clicked'},
        )
        files = [f for f in os.listdir(self.tmpdir) if f.endswith('.jsonl')]
        self.assertEqual(len(files), 1)

    def test_log_record_has_required_fields(self):
        self.logger.log(
            {'action': 'left_click', 'coordinate': [100, 200]},
            {'output': 'clicked', 'status': 'ok'},
            window_meta={'hwnd': 42, 'title': 'Test', 'process_name': 'test.exe',
                         'pid': 1234},
            screenshot_b64='ZmFrZS1pbWFnZS1ieXRlcw==',  # 'fake-image-bytes'
        )
        files = [f for f in os.listdir(self.tmpdir) if f.endswith('.jsonl')]
        with open(os.path.join(self.tmpdir, files[0])) as f:
            record = json.loads(f.readline())
        for key in ('ts', 'iso', 'action', 'coordinate', 'window',
                    'screenshot_sha256', 'status'):
            self.assertIn(key, record)
        self.assertEqual(record['action'], 'left_click')
        self.assertEqual(record['window']['hwnd'], 42)
        self.assertIsNotNone(record['screenshot_sha256'])

    def test_log_disabled_writes_nothing(self):
        cfg = SafetyConfig(audit_dir=self.tmpdir, audit_enabled=False)
        logger = AuditLogger(cfg)
        logger.log({'action': 'x'}, {'status': 'ok'})
        files = [f for f in os.listdir(self.tmpdir) if f.endswith('.jsonl')]
        self.assertEqual(len(files), 0)

    def test_block_reason_recorded(self):
        self.logger.log(
            {'action': 'left_click'}, {'status': 'safety_blocked'},
            block_reason='process_blocked: lsass.exe',
        )
        files = [f for f in os.listdir(self.tmpdir) if f.endswith('.jsonl')]
        with open(os.path.join(self.tmpdir, files[0])) as f:
            record = json.loads(f.readline())
        self.assertEqual(record['block_reason'], 'process_blocked: lsass.exe')

    def test_audit_dir_create_failure_disables_logger(self):
        """Bad audit_dir → log() must no-op, NOT raise."""
        bad_cfg = SafetyConfig(audit_dir='\x00invalid\x00path',
                               audit_enabled=True)
        logger = AuditLogger(bad_cfg)
        # Should not raise even though _ensure_dir failed.
        logger.log({'action': 'x'}, {'status': 'ok'})


class TestExecuteActionSafetyIntegration(unittest.TestCase):
    """execute_action(safety=True) wires the guards in correctly."""

    def setUp(self):
        # Reset singleton state between tests so action counts don't bleed.
        reset_session_guard()

    @patch('integrations.vlm.local_computer_tool._execute_inprocess')
    def test_safety_off_unchanged_behavior(self, mock_exec):
        """safety=False (default) → no guards, no audit, no block."""
        from integrations.vlm.local_computer_tool import execute_action
        mock_exec.return_value = {'output': 'ok'}
        result = execute_action({'action': 'left_click'}, 'inprocess')
        self.assertNotIn('safety_block', result)

    @patch('integrations.vlm.local_computer_tool._execute_inprocess')
    @patch('integrations.vlm.safety.is_window_blocked',
           return_value='process_blocked: bitwarden.exe')
    def test_blocked_window_refuses_with_safety(self, mock_block, mock_exec):
        """safety=True + blocklist hit → status='safety_blocked',
        _execute_inprocess never called."""
        from integrations.vlm.local_computer_tool import execute_action
        result = execute_action(
            {'action': 'left_click', 'coordinate': [100, 200]},
            'inprocess', safety=True)
        self.assertEqual(result['status'], 'safety_blocked')
        self.assertIn('bitwarden', result['safety_block'])
        mock_exec.assert_not_called()

    @patch('integrations.vlm.local_computer_tool._execute_inprocess')
    def test_session_cap_blocks_after_limit(self, mock_exec):
        """When the session cap is reached, further safety=True calls
        are refused."""
        from integrations.vlm.local_computer_tool import execute_action
        from integrations.vlm.safety import get_session_guard, SafetyConfig
        # Tighten the cap for the test.
        guard = get_session_guard()
        guard.config = SafetyConfig(max_actions_per_session=2,
                                     max_actions_per_second=100)
        mock_exec.return_value = {'output': 'ok'}
        # First two succeed.
        execute_action({'action': 'left_click'}, 'inprocess', safety=True)
        execute_action({'action': 'left_click'}, 'inprocess', safety=True)
        # Third blocks.
        result = execute_action({'action': 'left_click'},
                                'inprocess', safety=True)
        self.assertEqual(result['status'], 'safety_blocked')
        self.assertIn('session-cap', result['safety_block'])


if __name__ == '__main__':
    unittest.main()
