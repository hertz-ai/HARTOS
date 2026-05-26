"""Unit tests for the ``meet_copilot`` LiquidUI component type (UNIF-G5).

Verifies the new component-type allowlist entry accepts the documented
schema and the surrounding agent_ui_update path doesn't reject it.
Pure schema test — no transport, no frontend.
"""
from __future__ import annotations

import unittest


class MeetCopilotComponentTypeTest(unittest.TestCase):

    def test_meet_copilot_in_component_types(self):
        from integrations.agent_engine.liquid_ui_service import (
            COMPONENT_TYPES,
        )
        self.assertIn('meet_copilot', COMPONENT_TYPES)

    def test_meet_copilot_props_documented(self):
        # Props enumerated in the schema must cover everything the
        # MeetCopilotOverlay frontend renderer reads.
        from integrations.agent_engine.liquid_ui_service import (
            COMPONENT_TYPES,
        )
        props = set(COMPONENT_TYPES['meet_copilot']['props'])
        # Header
        self.assertIn('platform', props)
        self.assertIn('room_id', props)
        self.assertIn('state', props)
        self.assertIn('agent_role', props)
        # Body
        self.assertIn('transcript_lines', props)
        self.assertIn('decisions', props)
        self.assertIn('action_items', props)
        self.assertIn('participants', props)
        # Footer (leave action targets call_id)
        self.assertIn('call_id', props)

    def test_agent_ui_update_accepts_meet_copilot(self):
        # The agent_ui_update validator at line 363 rejects unknown
        # component types; this guards against accidental removal.
        from integrations.agent_engine.liquid_ui_service import (
            LiquidUIService,
        )
        svc = LiquidUIService()
        result = svc.agent_ui_update('meet_copilot_call_42', {
            'type': 'meet_copilot',
            'call_id': 'call_42',
            'platform': 'discord',
            'room_id': 'voice-room-7',
            'state': 'live',
            'transcript_lines': [
                {'speaker': 'alice', 'text': 'hello'},
                {'speaker': 'bob', 'text': 'hi there'},
            ],
            'decisions': ['use postgres'],
            'action_items': [{'text': 'sathish writes the migration'}],
            'participants': ['alice', 'bob', 'agent:nunba'],
            'agent_role': 'note_taker',
        })
        self.assertTrue(result)

    def test_agent_ui_update_rejects_typo(self):
        from integrations.agent_engine.liquid_ui_service import (
            LiquidUIService,
        )
        svc = LiquidUIService()
        # Typo ('meet_copliot') must be rejected
        result = svc.agent_ui_update('x', {
            'type': 'meet_copliot',
            'call_id': 'c',
            'platform': 'discord',
        })
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
