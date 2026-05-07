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


class MultiParticipantPresenceTest(unittest.TestCase):
    """N>2 participant scenarios for the agent-in-room contract.

    These extend the single-user happy-path tests above to assert
    the room_presence_service contract holds in real meetings — 3+
    humans, agent participating, only one objection needed to
    detach.

    No new code, no parallel paths: every primitive (gate,
    announce_presence, is_objection, listen_for_objection) is the
    SAME single source of truth used by 1-on-1 sessions.  The
    contract is symmetric; these tests just confirm the symmetry
    holds under fan-out.
    """

    def setUp(self):
        from integrations.social.room_presence_service import (
            is_objection, listen_for_objection, announce_presence,
        )
        self.is_objection = is_objection
        self.listen_for_objection = listen_for_objection
        self.announce_presence = announce_presence

    def _make_async_adapter(self):
        """Adapter mock that captures every send_message call so
        we can assert how many announcement messages went out."""
        adapter = MagicMock()
        adapter.name = 'discord'

        sent = []
        result = MagicMock(success=True, message_id='m')

        async def _send(chat_id, text, **kw):
            sent.append((chat_id, text))
            return result

        adapter.send_message = _send
        adapter._sent = sent
        return adapter

    def test_single_announcement_serves_all_participants(self):
        """Three participants in one room, agent joins as co-pilot:
        EXACTLY ONE announcement should be posted (the room broadcasts
        it to everyone — never per-participant).  Per-participant
        announcements would spam the room and double-count audit
        events."""
        from integrations.social import room_presence_service as rps
        adapter = self._make_async_adapter()
        with patch.object(rps, '_audit'):
            ok = rps.announce_presence(
                adapter, 'room-multi', 'user-owner', 'co_pilot',
                owner_display_name='Alice')
        self.assertTrue(ok)
        # ROOM-LEVEL announcement: exactly one send_message call,
        # not 3 (would mean it's iterating participants).
        self.assertEqual(
            len(adapter._sent), 1,
            'announce_presence must post ONE room-level message — '
            'a parallel per-participant loop would break audit + '
            'spam the room')

    def test_objection_from_any_participant_detaches(self):
        """In a 3-participant room, ANY participant typing an
        objection phrase must trigger detach.  We don't filter by
        speaker — agent-presence consent is a room-wide property,
        so any room member's objection is authoritative."""
        objection_cases = [
            ('alice', 'no ai please'),
            ('bob', 'sin ia'),       # Spanish speaker
            ('charlie', 'remove bot'),
            ('alice', 'ai venda'),    # Tamil speaker, same room
        ]
        for speaker, phrase in objection_cases:
            with self.subTest(speaker=speaker, phrase=phrase):
                self.assertTrue(
                    self.is_objection(phrase),
                    f'objection from {speaker}: {phrase!r} must trigger '
                    'detach regardless of speaker identity')

    def test_non_owner_participant_objection_still_authoritative(self):
        """A participant who is NOT the room owner can still object —
        the agent leaves on the FIRST objection, no priority based
        on who spoke.  Pinned to prevent a future "only owner can
        object" regression that would let the room owner override
        guest privacy concerns."""
        from integrations.social import room_presence_service as rps
        # Adapter that records the registered handler so we can
        # synthesise a non-owner objection through it.
        captured_handler = []
        adapter = MagicMock()
        adapter.name = 'matrix'

        def _on_message(handler):
            captured_handler.append(handler)

        adapter.on_message = _on_message
        adapter.send_message = MagicMock()

        detach_calls = []

        def _on_detach(reason):
            detach_calls.append(reason)

        with patch.object(rps, '_audit'):
            rps.listen_for_objection(
                adapter, 'room-multi', 'user-owner', 'agent-1',
                on_detach=_on_detach)

        self.assertEqual(len(captured_handler), 1,
                         'expected exactly one handler to register')
        handler = captured_handler[0]

        # Synthesise a message from a NON-owner participant.
        guest_message = MagicMock()
        guest_message.text = 'no ai please'
        guest_message.chat_id = 'room-multi'
        # Caller would post a real sender_id; we only care that the
        # objection text is recognised regardless of speaker.
        guest_message.sender_id = 'guest-bob'

        import asyncio
        # Run the async handler in a fresh event loop.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler(guest_message))
        finally:
            loop.close()

        # Detach callback fired exactly once with the canonical reason.
        self.assertEqual(detach_calls, ['participant_objection'],
                         'non-owner objection must trigger the same '
                         'detach path as owner objection')

    def test_first_objection_wins_no_re_objection_needed(self):
        """If a room has 3 objection-eligible phrases in flight (two
        speakers happen to type opt-out at the same time), the
        on_detach callback runs ONCE per objection event — but since
        the agent leaves on the first one, the second event lands on
        a stale handler.  That's the OS responsibility (adapter
        unregister); presence_service must not double-detach the
        agent on its own.  Pinned by counting on_detach invocations
        across two synthesised objections in sequence."""
        from integrations.social import room_presence_service as rps
        captured_handler = []
        adapter = MagicMock()
        adapter.name = 'slack'

        def _on_message(h):
            captured_handler.append(h)

        adapter.on_message = _on_message
        adapter.send_message = MagicMock()

        detach_calls = []

        with patch.object(rps, '_audit'):
            rps.listen_for_objection(
                adapter, 'room-multi', 'user-owner', 'agent-1',
                on_detach=lambda reason: detach_calls.append(reason))

        handler = captured_handler[0]

        import asyncio

        async def _send_two_objections():
            m1 = MagicMock(text='no ai', chat_id='room-multi',
                           sender_id='alice')
            m2 = MagicMock(text='kick the bot', chat_id='room-multi',
                           sender_id='bob')
            await handler(m1)
            await handler(m2)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send_two_objections())
        finally:
            loop.close()

        # Both messages match → on_detach fires per objection.  This
        # documents EXISTING behaviour (no dedupe inside presence
        # service); the agent's actual one-shot detach lives in the
        # caller (G2 Join_External_Room agent tool) which calls
        # leave_room exactly once.  Pinning the count here so a
        # future change adds dedupe deliberately, not silently.
        self.assertEqual(
            len(detach_calls), 2,
            'on_detach fires once per objection event — caller is '
            'responsible for one-shot leave_room.  If this grows to '
            '1 (silent dedupe added), update the docstring of '
            'listen_for_objection AND add a regression test for the '
            'one-shot caller path so the contract stays explicit.')

    def test_objection_for_different_room_id_is_ignored(self):
        """The watcher filters by ``room_id`` — an objection in a
        DIFFERENT room (same adapter, different chat_id) MUST NOT
        detach the agent from THIS room.  Pinned to prevent a
        regression where one user's objection in a side-channel
        room kicks the agent out of an unrelated meeting."""
        from integrations.social import room_presence_service as rps
        captured_handler = []
        adapter = MagicMock()
        adapter.name = 'telegram'

        def _on_message(h):
            captured_handler.append(h)

        adapter.on_message = _on_message
        adapter.send_message = MagicMock()

        detach_calls = []

        with patch.object(rps, '_audit'):
            rps.listen_for_objection(
                adapter, 'room-A', 'user-owner', 'agent-1',
                on_detach=lambda reason: detach_calls.append(reason))

        handler = captured_handler[0]

        # Objection in OTHER room — must be ignored.
        wrong_room_msg = MagicMock(text='no ai',
                                    chat_id='room-B',
                                    sender_id='alice')
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler(wrong_room_msg))
        finally:
            loop.close()

        self.assertEqual(
            detach_calls, [],
            'objection in different room must NOT detach this agent — '
            'cross-room contamination would let an unrelated room '
            'kick the agent out of a current meeting')


if __name__ == '__main__':
    unittest.main()
