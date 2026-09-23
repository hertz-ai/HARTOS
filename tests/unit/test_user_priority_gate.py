"""#123 — verify the user-priority gate (should_yield_to_user) engages.

Live evidence (2026-05-20 frozen_debug.log + earlier): daemon ticks
hammered the backend while the user was actively chatting.  The
canonical gate at integrations/agent_engine/dispatch.py:151 should
return True for at least USER_CHAT_COOLDOWN seconds after the last
mark_user_chat_activity() call.

This test pins the contract so a future refactor of dispatch.py can't
silently disable the gate.

Subject under test:
    integrations.agent_engine.dispatch
      - mark_user_chat_activity()
      - mark_create_start() / mark_create_end()
      - is_user_recently_active()
      - should_yield_to_user()
"""
import time
import unittest
from unittest.mock import patch


class UserPriorityGateTests(unittest.TestCase):
    def setUp(self):
        from integrations.agent_engine import dispatch
        self.dispatch = dispatch
        # Reset module-level state so each test starts clean.
        dispatch._last_user_chat_at = 0
        dispatch._active_create_sessions = 0

    def test_gate_engages_immediately_after_user_chat(self):
        self.dispatch.mark_user_chat_activity()
        self.assertTrue(self.dispatch.is_user_recently_active())
        self.assertTrue(self.dispatch.should_yield_to_user())

    def test_gate_disengages_after_cooldown(self):
        # Set _last_user_chat_at to before the cooldown window.
        self.dispatch._last_user_chat_at = time.time() - (
            self.dispatch._USER_CHAT_COOLDOWN + 5)
        self.assertFalse(self.dispatch.is_user_recently_active())
        # should_yield may still return True due to system-pressure
        # reasons; we explicitly want to assert user-activity branch
        # is False.

    def test_create_session_keeps_gate_engaged_indefinitely(self):
        self.dispatch.mark_create_start()
        # Even after the chat-cooldown window expires, an active CREATE
        # keeps the gate engaged — recipe synthesis is more expensive
        # than a regular chat turn.
        self.dispatch._last_user_chat_at = time.time() - 3600  # 1h ago
        self.assertTrue(self.dispatch.is_user_recently_active())
        self.assertTrue(self.dispatch.should_yield_to_user())
        # Cleanup
        self.dispatch.mark_create_end()
        self.assertEqual(self.dispatch._active_create_sessions, 0)

    def test_create_start_also_marks_chat_activity(self):
        """Bug fix from mid-session: mark_create_start MUST also call
        mark_user_chat_activity, else a CREATE that runs longer than
        the chat-cooldown window can let daemons hammer mid-pipeline."""
        # Pass a GENUINE USER request id: stamping became conditional on
        # is_genuine_user_request() so a DAEMON-initiated create can no longer
        # re-arm the 10-min user cooldown (that fed the starvation-override ->
        # forced tick -> daemon create -> re-stamp loop and kept the yield gate
        # stuck on). The no-id call this test used is therefore NOT the user
        # case it is about.
        before = self.dispatch._last_user_chat_at
        self.dispatch.mark_create_start(request_id='user_42_1700000000')
        after = self.dispatch._last_user_chat_at
        self.assertGreater(after, before)
        self.dispatch.mark_create_end()

    def test_daemon_create_start_does_not_rearm_user_cooldown(self):
        """The other half of the same rule: a daemon create must NOT stamp."""
        before = self.dispatch._last_user_chat_at
        self.dispatch.mark_create_start(request_id='daemon_goal7')
        self.assertEqual(self.dispatch._last_user_chat_at, before)
        self.dispatch.mark_create_end()

    def test_concurrent_create_sessions_counted_correctly(self):
        self.dispatch.mark_create_start()
        self.dispatch.mark_create_start()
        self.assertEqual(self.dispatch._active_create_sessions, 2)
        self.dispatch.mark_create_end()
        self.assertEqual(self.dispatch._active_create_sessions, 1)
        self.dispatch.mark_create_end()
        self.assertEqual(self.dispatch._active_create_sessions, 0)
        # mark_create_end never goes negative
        self.dispatch.mark_create_end()
        self.assertEqual(self.dispatch._active_create_sessions, 0)

    def test_gate_yields_on_system_pressure(self):
        """Independent of user activity, very-low LLM throttle_factor
        also triggers the gate (cooperate with the model_lifecycle
        manager — don't hammer when system is hot)."""
        # User NOT recently active
        self.dispatch._last_user_chat_at = 0
        self.dispatch._active_create_sessions = 0

        # Stub model_lifecycle to report critical pressure
        with patch(
                'integrations.service_tools.model_lifecycle.'
                'get_model_lifecycle_manager') as mock_mgr:
            mock_mgr.return_value.get_system_pressure.return_value = {
                'throttle_factor': 0.05,  # below 0.1 trigger
            }
            self.assertTrue(self.dispatch.should_yield_to_user())

    def test_gate_yields_on_resource_governor_pressure(self):
        """Third yield reason: generic CPU/RAM pressure picked up by
        the resource governor (not LLM-shaped, e.g. runaway Python
        loop) — gate must still engage."""
        self.dispatch._last_user_chat_at = 0
        self.dispatch._active_create_sessions = 0

        with patch(
                'core.resource_governor.get_governor') as mock_gov:
            mock_gov.return_value.get_throttle.return_value = 0.2  # < 0.3
            # Stub model_lifecycle to NOT trigger
            with patch(
                    'integrations.service_tools.model_lifecycle.'
                    'get_model_lifecycle_manager') as mock_mgr:
                mock_mgr.return_value.get_system_pressure.return_value = {
                    'throttle_factor': 0.9,
                }
                self.assertTrue(self.dispatch.should_yield_to_user())

    def test_gate_does_not_yield_when_system_calm_and_user_idle(self):
        """All clear — daemons can tick."""
        self.dispatch._last_user_chat_at = 0
        self.dispatch._active_create_sessions = 0

        with patch(
                'integrations.service_tools.model_lifecycle.'
                'get_model_lifecycle_manager') as mock_mgr, \
             patch('core.resource_governor.get_governor') as mock_gov:
            mock_mgr.return_value.get_system_pressure.return_value = {
                'throttle_factor': 0.9,
            }
            mock_gov.return_value.get_throttle.return_value = 0.9
            self.assertFalse(self.dispatch.should_yield_to_user())


class GateConsumerWiringTests(unittest.TestCase):
    """All 4 daemons must call should_yield_to_user — pin that with
    a code-search test so a refactor that drops the call gets caught."""

    def test_agent_daemon_consults_gate(self):
        import inspect
        from integrations.agent_engine import agent_daemon
        src = inspect.getsource(agent_daemon)
        self.assertIn(
            'should_yield_to_user', src,
            'agent_daemon must call should_yield_to_user before '
            'firing background ticks (#123).'
        )

    def test_coding_daemon_consults_gate(self):
        try:
            from integrations.coding_agent import coding_daemon
        except ImportError:
            self.skipTest('coding_daemon not importable in test env')
        import inspect
        src = inspect.getsource(coding_daemon)
        self.assertIn(
            'should_yield_to_user', src,
            'coding_daemon must call should_yield_to_user (#123).'
        )


class UserChatMarkerTests(unittest.TestCase):
    """The user-chat marker: the cross-process copy of _last_user_chat_at.

    hart-agent-daemon runs in its own process, where this module's timestamp
    is the daemon's own never-stamped copy, so is_user_recently_active()
    could never say yes there.  mark_user_chat_activity now also touches
    user-chat.<pid> in the session marker dir (core.foreground, the same
    idiom as the governor's input-alive reader) and the reader falls back to
    the youngest live FOREIGN marker only when the in-process timestamp was
    never set.  The tests above run with no marker dir and are unchanged.
    """

    def setUp(self):
        import os
        import tempfile
        from integrations.agent_engine import dispatch
        self.dispatch = dispatch
        self.os = os
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_env = os.environ.get('HART_SESSION_MARKER_DIR')
        os.environ['HART_SESSION_MARKER_DIR'] = self.tmp.name
        dispatch._last_user_chat_at = 0
        dispatch._active_create_sessions = 0

    def tearDown(self):
        if self.prev_env is None:
            self.os.environ.pop('HART_SESSION_MARKER_DIR', None)
        else:
            self.os.environ['HART_SESSION_MARKER_DIR'] = self.prev_env
        self.dispatch._last_user_chat_at = 0
        self.tmp.cleanup()

    def _marker(self, pid):
        return self.os.path.join(self.tmp.name, f'user-chat.{pid}')

    def test_mark_touches_the_marker_for_this_process(self):
        self.dispatch.mark_user_chat_activity()
        self.assertTrue(self.os.path.exists(self._marker(self.os.getpid())))

    # The two-process proof (a child serves the chat, this process is the
    # daemon) lives in test_foreground_yield.py beside its foreground twin,
    # which owns the child-process plumbing.  These pin the reader's rules.

    def test_foreign_marker_is_read_and_a_dead_writer_still_counts(self):
        """A marker from a pid that no longer exists (a backend restart after
        the chat) still says the person chatted: the writer need not be alive,
        unlike a foreground marker, because the fact is about the person."""
        import subprocess
        import sys
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait(timeout=60)
        open(self._marker(p.pid), 'a').close()
        self.assertTrue(self.dispatch.is_user_recently_active())
        self.assertTrue(self.dispatch.should_yield_to_user())
        self.assertEqual(self.dispatch.get_last_yield_reason(), 'user_active')

    def test_stale_marker_reads_inactive(self):
        """Older than the 10 minute window: the other process's chat is
        over, exactly as its own timestamp would have said."""
        import time
        p = self._marker(self.os.getppid())
        open(p, 'a').close()
        stale = time.time() - (self.dispatch._USER_CHAT_COOLDOWN + 5)
        self.os.utime(p, (stale, stale))
        self.assertFalse(self.dispatch.is_user_recently_active())
        self.os.utime(p, None)
        self.assertTrue(self.dispatch.is_user_recently_active())

    def test_own_marker_is_not_evidence(self):
        """A marker this process wrote is this process's own state, which
        the caller has just said is nothing: the reader ignores it, so a
        reset in-process timestamp means what it says."""
        self.dispatch.mark_user_chat_activity()
        self.dispatch._last_user_chat_at = 0
        self.assertFalse(self.dispatch.is_user_recently_active())

    def test_marker_consulted_only_when_the_timestamp_says_nothing(self):
        """An in-process timestamp, even a stale one, is the answer: this
        process is the writer and its own memory wins over a file."""
        import time
        p = self._marker(self.os.getppid())
        open(p, 'a').close()  # a fresh foreign chat
        self.dispatch._last_user_chat_at = time.time() - (
            self.dispatch._USER_CHAT_COOLDOWN + 5)
        self.assertFalse(self.dispatch.is_user_recently_active())

    def test_create_in_flight_still_wins_without_any_file_read(self):
        self.dispatch.mark_create_start()
        try:
            with patch('core.foreground.marker_age_s',
                       side_effect=AssertionError('marker read')):
                self.assertTrue(self.dispatch.is_user_recently_active())
        finally:
            self.dispatch.mark_create_end()


if __name__ == '__main__':
    unittest.main()
