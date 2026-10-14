"""Unit tests for ``chat_messages.persist_external_room_event`` (UNIF-G3).

Pure shape tests — no DB, no adapter, no live transport.  Verifies that
the helper produces the canonical field mapping documented in its
docstring so adapters can rely on it for chronological cross-channel
recall.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class PersistExternalRoomEventTest(unittest.TestCase):

    def setUp(self):
        from integrations.social import chat_messages as cm
        self.cm = cm

    def _capture_call(self, *args, **kwargs):
        # Capture every persist_and_publish_async invocation so the
        # tests can assert on its kwargs without hitting the DB.
        self.captured.append((args, kwargs))

    def test_canonical_shape_for_text_message(self):
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='owner-1',
                platform='discord',
                room_id='channel-42',
                sender_id='alice',
                text='hello from discord',
                timestamp=1234567890.0,
                lang='en',
                msg_id='msg-1',
            )
        self.assertEqual(len(self.captured), 1)
        args, kwargs = self.captured[0]
        # Positional: user_id, role, content
        self.assertEqual(args[0], 'owner-1')
        self.assertEqual(args[1], 'user')   # auto-derived (sender != agent:*)
        self.assertEqual(args[2], 'hello from discord')
        # Composite channel_type encodes BOTH platform + room
        self.assertEqual(kwargs['channel_type'], 'discord:channel-42')
        # request_id groups by room
        self.assertEqual(kwargs['request_id'], 'channel-42')
        # device_id tells consumer this is adapter-side
        self.assertEqual(kwargs['device_id'], 'adapter:discord')
        # Provenance attachment
        att = kwargs['attachments'][0]
        self.assertEqual(att['kind'], 'external_room_msg')
        self.assertEqual(att['platform'], 'discord')
        self.assertEqual(att['room_id'], 'channel-42')
        self.assertEqual(att['author'], 'alice')
        self.assertEqual(att['t'], 1234567890.0)

    def test_role_assistant_when_sender_is_agent(self):
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='owner-1',
                platform='slack',
                room_id='C123',
                sender_id='agent:nunba-co-pilot',
                text='here is the summary',
            )
        args, kwargs = self.captured[0]
        self.assertEqual(args[1], 'assistant')

    def test_role_explicit_overrides_heuristic(self):
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='owner-1',
                platform='matrix',
                room_id='!room:matrix.org',
                sender_id='someone',
                text='thinking…',
                role='system',
            )
        args, _ = self.captured[0]
        self.assertEqual(args[1], 'system')

    def test_transcript_segment_kind_carries_extra(self):
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='owner-1',
                platform='discord',
                room_id='voice-room-7',
                sender_id='alice',
                text='hello there',
                kind='transcript_segment',
                extra={'t0': 12.5, 't1': 13.8, 'speaker': 'alice'},
            )
        _, kwargs = self.captured[0]
        att = kwargs['attachments'][0]
        self.assertEqual(att['kind'], 'transcript_segment')
        self.assertEqual(att['t0'], 12.5)
        self.assertEqual(att['t1'], 13.8)
        self.assertEqual(att['speaker'], 'alice')
        # Author is still preserved
        self.assertEqual(att['author'], 'alice')

    def test_extra_does_not_overwrite_canonical_fields(self):
        # extra must NEVER clobber kind/platform/room_id/author —
        # those come from named args.
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='owner-1',
                platform='discord',
                room_id='room-1',
                sender_id='alice',
                text='hi',
                extra={'platform': 'WRONG', 'kind': 'WRONG',
                       'room_id': 'WRONG', 'author': 'WRONG'},
            )
        _, kwargs = self.captured[0]
        att = kwargs['attachments'][0]
        self.assertEqual(att['platform'], 'discord')
        self.assertEqual(att['kind'], 'external_room_msg')
        self.assertEqual(att['room_id'], 'room-1')
        self.assertEqual(att['author'], 'alice')

    def test_missing_required_fields_no_op(self):
        # Empty text / missing room / missing platform → silent no-op
        # (no exception, no persist call).
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='', platform='x', room_id='y',
                sender_id='z', text='nothing')
            self.cm.persist_external_room_event(
                user_id='u', platform='', room_id='y',
                sender_id='z', text='nothing')
            self.cm.persist_external_room_event(
                user_id='u', platform='x', room_id='',
                sender_id='z', text='nothing')
            self.cm.persist_external_room_event(
                user_id='u', platform='x', room_id='y',
                sender_id='z', text='')
        self.assertEqual(len(self.captured), 0)

    def test_chronological_recall_orderable(self):
        # When multiple events from different platforms come in, the
        # attachment 't' field + content carry enough info that a
        # downstream recall can sort them chronologically.  This test
        # verifies the helper doesn't lose the timestamp.
        self.captured = []
        with patch.object(self.cm, 'persist_and_publish_async',
                          side_effect=self._capture_call):
            self.cm.persist_external_room_event(
                user_id='u', platform='whatsapp', room_id='g1',
                sender_id='a', text='one', timestamp=1000.0)
            self.cm.persist_external_room_event(
                user_id='u', platform='discord', room_id='c1',
                sender_id='b', text='two', timestamp=2000.0)
            self.cm.persist_external_room_event(
                user_id='u', platform='telegram', room_id='t1',
                sender_id='c', text='three', timestamp=3000.0)
        timestamps = [c[1]['attachments'][0].get('t') for c in self.captured]
        self.assertEqual(timestamps, [1000.0, 2000.0, 3000.0])
        platforms = [c[1]['attachments'][0]['platform'] for c in self.captured]
        self.assertEqual(platforms, ['whatsapp', 'discord', 'telegram'])


class PublishNewTest(unittest.TestCase):
    """A persisted turn goes to its own user only, on every leg.

    publish_new handed the bus the raw URI with no user: the SSE leg
    broadcast it to every connected user's stream (live 2026-10-03:
    `broadcast_sse_event: type=com.hertzai.hevolve.chat.new.10202,
    user_id=None, targeted=4, total_clients=4`, three users' turns), and
    the Crossbar leg found no TOPIC_MAP entry for the URI and sent nothing,
    so no WAMP subscriber of chat.new.<uid> ever received a turn.
    """

    ROW = {'user_id': 'u-7', 'role': 'user', 'content': 'hi',
           'msg_id': '0123456789abcdef', 'device_id': 'phone-1'}

    def test_sse_and_crossbar_both_address_the_rows_user(self):
        from core.peer_link.message_bus import MessageBus
        from integrations.social import chat_messages as cm
        bus = MessageBus()
        sse, crossbar = [], []
        bus.set_http_transport(lambda topic, payload: crossbar.append(topic))

        def _sse(event_type, data, user_id=None):
            sse.append((event_type, user_id))
            return True

        with patch('core.peer_link.message_bus.get_message_bus',
                   return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe', _sse):
            cm.publish_new(dict(self.ROW))

        self.assertEqual(sse, [('chat.new', 'u-7')])
        self.assertEqual(crossbar, ['com.hertzai.hevolve.chat.new.u-7'])

    def test_the_row_keeps_its_own_msg_id_on_every_leg(self):
        """Clients dedup a turn by the row's msg_id (the cursor pull returns
        it too).  The Crossbar leg replaced it with the bus's own id, so the
        same turn reached a page under two ids, one per transport."""
        import json
        from core.peer_link.message_bus import MessageBus
        from integrations.social import chat_messages as cm
        bus = MessageBus()
        sse, crossbar = [], []
        bus.set_http_transport(
            lambda topic, payload: crossbar.append(json.loads(payload)))

        def _sse(event_type, data, user_id=None):
            sse.append(data)
            return True

        with patch('core.peer_link.message_bus.get_message_bus',
                   return_value=bus), \
                patch('core.platform.events.broadcast_sse_safe', _sse):
            cm.publish_new(dict(self.ROW))

        self.assertEqual(sse[0]['msg_id'], self.ROW['msg_id'])
        self.assertEqual(crossbar[0]['msg_id'], self.ROW['msg_id'])

        # This node's own publish coming back from the router is an echo:
        # it is not delivered to local subscribers a second time.
        got = []
        bus.subscribe('chat.new', lambda t, d: got.append(d))
        self.assertFalse(bus.receive_from_crossbar(
            'com.hertzai.hevolve.chat.new.u-7', dict(crossbar[0])))
        self.assertEqual(got, [])


if __name__ == '__main__':
    unittest.main()
