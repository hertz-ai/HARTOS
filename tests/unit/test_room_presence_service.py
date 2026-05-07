"""Unit tests for ``integrations.social.room_presence_service`` (UNIF-G6).

Per ``feedback_test_what_we_ship.md`` the new module ships with tests
that exercise its pure helpers + audit hooks.  The interactive paths
(real ChannelAdapter join, ConsentService DB) are exercised by the
adapter-level tests in tests/integration/ later.

Coverage targets here:
  - ``is_objection`` — i18n phrase matcher, no side effects
  - ``_scope_for_role`` — role → scope mapping (private but stable)
  - ``gate`` — denies on missing consent, audit log fired
  - ``announce_presence`` — uses adapter.send_message, audits success/fail
  - ``listen_for_objection`` — registers handler; objection triggers detach
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class IsObjectionTest(unittest.TestCase):
    """Pure-function matcher; no DB, no adapter."""

    def setUp(self):
        from integrations.social.room_presence_service import is_objection
        self.is_objection = is_objection

    def test_empty_or_none(self):
        self.assertFalse(self.is_objection(''))
        self.assertFalse(self.is_objection(None))

    def test_unrelated_text(self):
        self.assertFalse(self.is_objection('hello world'))
        self.assertFalse(self.is_objection('let us discuss the project'))

    def test_english_phrases(self):
        for p in ('no AI', 'No Ai please', 'kick the bot',
                  '/agent-out', 'remove ai now', 'no agent'):
            with self.subTest(phrase=p):
                self.assertTrue(self.is_objection(p))

    def test_multilingual(self):
        # Spot-check at least one phrase from several supported langs.
        cases = [
            'sin ia',          # es
            "pas d'ia",        # fr
            'keine ki',        # de
            'sem ia',          # pt
            'wu ai',           # zh transliteration
            'ai venda',        # ta latin
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertTrue(self.is_objection(phrase))

    def test_case_insensitive(self):
        self.assertTrue(self.is_objection('NO AI'))
        self.assertTrue(self.is_objection('No Ai'))


class ScopeForRoleTest(unittest.TestCase):

    def test_known_roles_map_to_distinct_scopes(self):
        from integrations.social.room_presence_service import _scope_for_role
        self.assertEqual(_scope_for_role('note_taker'),
                         'agent_listens_external_audio')
        self.assertEqual(_scope_for_role('writer'),
                         'agent_writes_external_room')
        self.assertEqual(_scope_for_role('co_pilot'),
                         'agent_joins_external_room')
        self.assertEqual(_scope_for_role('participant'),
                         'agent_joins_external_room')

    def test_unknown_role_falls_back(self):
        from integrations.social.room_presence_service import _scope_for_role
        # Unknown role → default scope, not an error
        self.assertEqual(_scope_for_role('intergalactic_overlord'),
                         'agent_joins_external_room')
        self.assertEqual(_scope_for_role(''), 'agent_joins_external_room')


class GateTest(unittest.TestCase):
    """``gate`` is the consent decision point.  Fails-closed on errors."""

    def test_consent_unavailable_fails_closed(self):
        from integrations.social import room_presence_service as rps
        # Force the import inside gate() to raise — sim DB outage.
        with patch.dict('sys.modules',
                        {'integrations.social.consent_service': None}):
            allowed, reason = rps.gate('user-1', 'discord', 'room-1', 'co_pilot')
        self.assertFalse(allowed)
        self.assertIn('unavailable', reason.lower())

    def test_grant_allows(self):
        from integrations.social import room_presence_service as rps
        with patch('integrations.social.consent_service.ConsentService.check_consent',
                   return_value=True), \
             patch('integrations.social.models.get_db') as mock_get_db, \
             patch.object(rps, '_audit') as mock_audit:
            mock_get_db.return_value = MagicMock()
            allowed, reason = rps.gate('user-1', 'discord', 'room-1', 'co_pilot')
        self.assertTrue(allowed)
        self.assertEqual(reason, 'ok')
        # Audit log was fired with action='allowed'
        actions = [c.args[2] for c in mock_audit.call_args_list]
        self.assertIn('allowed', actions)

    def test_no_consent_denies_with_reason(self):
        from integrations.social import room_presence_service as rps
        with patch('integrations.social.consent_service.ConsentService.check_consent',
                   return_value=False), \
             patch('integrations.social.models.get_db') as mock_get_db, \
             patch.object(rps, '_audit') as mock_audit:
            mock_get_db.return_value = MagicMock()
            allowed, reason = rps.gate('user-1', 'discord', 'room-1', 'note_taker')
        self.assertFalse(allowed)
        # The reason mentions the right scope so the UI prompt can
        # surface it directly.
        self.assertIn('agent_listens_external_audio', reason)
        actions = [c.args[2] for c in mock_audit.call_args_list]
        self.assertIn('denied_no_consent', actions)


class AnnouncePresenceTest(unittest.TestCase):
    """``announce_presence`` posts via adapter.send_message + audits."""

    def _make_adapter(self, *, success=True):
        adapter = MagicMock()
        adapter.name = 'discord'
        result = MagicMock()
        result.success = success
        result.message_id = 'msg-123'

        async def _send(chat_id, text, **kwargs):
            self.last_text = text
            self.last_chat_id = chat_id
            return result

        adapter.send_message = _send
        return adapter

    def test_announce_posts_disclosure_text(self):
        from integrations.social import room_presence_service as rps
        adapter = self._make_adapter()
        with patch.object(rps, '_audit') as mock_audit:
            ok = rps.announce_presence(
                adapter, 'room-1', 'user-1', 'co_pilot',
                owner_display_name='Alice')
        self.assertTrue(ok)
        # Disclosure includes role + opt-out instructions.
        self.assertIn('Alice', self.last_text)
        self.assertIn('co-pilot', self.last_text)
        self.assertIn('no AI', self.last_text)
        # Audit fired with 'announced'.
        actions = [c.args[2] for c in mock_audit.call_args_list]
        self.assertIn('announced', actions)


class ListenForObjectionTest(unittest.TestCase):

    def test_handler_registered(self):
        from integrations.social import room_presence_service as rps
        adapter = MagicMock()
        rps.listen_for_objection(adapter, 'room-1', 'user-1', 'agent-1')
        self.assertTrue(adapter.on_message.called)

    def test_no_op_when_adapter_has_no_on_message(self):
        from integrations.social import room_presence_service as rps
        # Adapter without on_message attribute — should not raise.
        class NoHookAdapter:
            pass
        rps.listen_for_objection(NoHookAdapter(), 'room-1', 'user-1', 'agent-1')


if __name__ == '__main__':
    unittest.main()
