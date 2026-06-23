"""Regression: canned chat PROGRESS must not masquerade as LLM reasoning.

Live UX bug (steward, latest build): the "Thought process" container showed
``Step 1 Loading your context… / Step 2 Recalling our recent chat… / Step 3
Preparing tools… / Step 4 Thinking…`` instead of the model's actual thoughts.

Root cause: ``publish_chat_stage`` (canned CHAT_STAGE_TEXTS milestones) and
``model_bus_service._publish_routing_status`` (canned routing progress) both
routed through ``publish_thinking_trace`` on the priority-49 / action='Thinking'
channel — the very channel the frontend (Demopage: priority===49 &&
action==='Thinking') appends to ``thinkingSteps``.  So progress chatter rendered
as Thought-process Steps and drowned out / stood in for real reasoning.

Fix: ``publish_thinking_trace`` takes an ``action`` (default 'Thinking'); the two
canned publishers pass ``action='Status'``.  The frontend routes 'Status' to the
"analysing…" spinner only, reserving the priority-49 'Thinking' container for the
model's actual reasoning.

Behavioural: real publishers, the publish_async boundary mocked; assert the wire
envelope's ``action``/``priority`` — the exact fields the frontend keys on.

    python -m pytest tests/unit/test_thinking_vs_status_split.py --noconftest -q
"""
import json
import unittest
from unittest.mock import patch


class TestThinkingVsStatusSplit(unittest.TestCase):
    def _capture(self, fn, *args, **kwargs):
        """Invoke a publisher with publish_async mocked; return (ok, envelope)."""
        sent = {}

        def _fake_publish_async(topic, payload):
            sent['topic'] = topic
            sent['envelope'] = json.loads(payload)

        # crossbar_publish resolves the publisher via
        # safe_hartos_attr('publish_async') at call time — patch that source.
        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=_fake_publish_async):
            ok = fn(*args, **kwargs)
        return ok, sent.get('envelope')

    def test_real_reasoning_defaults_to_Thinking_priority49(self):
        from core.peer_link.crossbar_publish import publish_thinking_trace
        ok, env = self._capture(
            publish_thinking_trace, text='the model is reasoning…', user_id='u1')
        self.assertTrue(ok)
        self.assertEqual(env['action'], 'Thinking')
        self.assertEqual(env['priority'], 49)

    def test_chat_stage_rides_Status_not_Thinking(self):
        from core.peer_link.crossbar_publish import publish_chat_stage
        ok, env = self._capture(
            publish_chat_stage, 'loading_context', user_id='u1')
        self.assertTrue(ok)
        # Canned milestone → must NOT be a Thought-process Step.
        self.assertEqual(env['action'], 'Status')
        self.assertNotEqual(env['action'], 'Thinking')
        # Text still resolves from CHAT_STAGE_TEXTS so the spinner has a label.
        self.assertIn('Loading your context', env['text'][0])

    def test_routing_status_rides_Status_not_Thinking(self):
        from integrations.agent_engine.model_bus_service import (
            _publish_routing_status)
        # _publish_routing_status is fire-and-forget (returns None); assert the
        # wire envelope it produced, not a return value.
        _, env = self._capture(
            _publish_routing_status, 'u1', 'Checking hive network…', 'req-1')
        self.assertIsNotNone(env)
        self.assertEqual(env['action'], 'Status')
        self.assertEqual(env['bot_type'], 'ComputeRouter')

    def test_explicit_action_is_honored_on_both_envelope_shapes(self):
        from core.peer_link.crossbar_publish import publish_thinking_trace
        # full_schema=False (the publish_chat_stage path)
        _, env_small = self._capture(
            publish_thinking_trace, text='x', user_id='u1',
            full_schema=False, action='Status')
        self.assertEqual(env_small['action'], 'Status')
        # full_schema=True (the autogen/action-start tap path)
        _, env_full = self._capture(
            publish_thinking_trace, text='x', user_id='u1',
            full_schema=True, action='Status')
        self.assertEqual(env_full['action'], 'Status')


if __name__ == '__main__':
    unittest.main()
