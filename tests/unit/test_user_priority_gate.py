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
        before = self.dispatch._last_user_chat_at
        self.dispatch.mark_create_start()
        after = self.dispatch._last_user_chat_at
        self.assertGreater(after, before)
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


if __name__ == '__main__':
    unittest.main()
