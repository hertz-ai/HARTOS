"""[[MEDIA:path]] markers on the channel reply path — leg C of #752.

The registry's reply leg was text-only (registry.py:_route_to_agent), so a
tool that produced a file (generate_receipt render='image') had no way to
put it on the wire.  These tests pin the marker contract: markers become
MediaAttachments, are stripped from the text, and anything outside the
app's own data dir is dropped — the marker rides LLM output, so an
arbitrary path would let a prompted model attach any file on disk.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from integrations.channels.base import (
    ChannelStatus,
    Message,
    MessageType,
    SendResult,
)
from integrations.channels.registry import (
    ChannelRegistry,
    extract_media_markers,
)


class ExtractMediaMarkers(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='media_root_')
        self.inside = os.path.join(self.root, 'receipts', 'r1.png')
        os.makedirs(os.path.dirname(self.inside), exist_ok=True)
        with open(self.inside, 'wb') as f:
            f.write(b'\x89PNG fake')
        self._p = patch(
            'integrations.channels.registry._media_allowlist_root',
            return_value=os.path.realpath(self.root))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_marker_becomes_attachment_and_text_is_cleaned(self):
        text = f'Here is your receipt.\n[[MEDIA:{self.inside}]]'
        clean, media = extract_media_markers(text)
        self.assertEqual(clean, 'Here is your receipt.')
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, MessageType.IMAGE)
        self.assertEqual(media[0].mime_type, 'image/png')
        self.assertEqual(os.path.realpath(media[0].file_path),
                         os.path.realpath(self.inside))

    def test_path_outside_data_dir_is_dropped_but_marker_still_stripped(self):
        outside = os.path.join(tempfile.gettempdir(), 'secrets.png')
        with open(outside, 'wb') as f:
            f.write(b'x')
        self.addCleanup(lambda: os.remove(outside))
        text = f'reply [[MEDIA:{outside}]]'
        clean, media = extract_media_markers(text)
        self.assertEqual(media, [])
        self.assertNotIn('[[MEDIA:', clean)

    def test_missing_file_is_dropped(self):
        text = f'reply [[MEDIA:{os.path.join(self.root, "gone.png")}]]'
        clean, media = extract_media_markers(text)
        self.assertEqual(media, [])

    def test_unknown_extension_is_dropped(self):
        exe = os.path.join(self.root, 'evil.exe')
        with open(exe, 'wb') as f:
            f.write(b'MZ')
        clean, media = extract_media_markers(f'x [[MEDIA:{exe}]]')
        self.assertEqual(media, [])

    def test_plain_text_untouched(self):
        clean, media = extract_media_markers('no markers here')
        self.assertEqual(clean, 'no markers here')
        self.assertEqual(media, [])


class _RecordingAdapter:
    """Minimal adapter stub recording send_message kwargs."""

    name = 'stub'

    def __init__(self):
        self.sent = []

    def on_message(self, cb):
        pass

    def get_status(self):
        return ChannelStatus.CONNECTED

    def is_running(self):
        return True

    async def send_typing(self, chat_id):
        pass

    async def send_message(self, chat_id, text, reply_to=None,
                           media=None, buttons=None):
        self.sent.append({'chat_id': chat_id, 'text': text,
                          'reply_to': reply_to, 'media': media})
        return SendResult(success=True)


class RouteToAgentCarriesMedia(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='media_root_')
        self.png = os.path.join(self.root, 'r.png')
        with open(self.png, 'wb') as f:
            f.write(b'\x89PNG fake')
        self._p = patch(
            'integrations.channels.registry._media_allowlist_root',
            return_value=os.path.realpath(self.root))
        self._p.start()
        self.addCleanup(self._p.stop)

    def _run(self, response_text):
        registry = ChannelRegistry()
        adapter = _RecordingAdapter()
        registry._adapters['stub'] = adapter
        registry.set_agent_handler(lambda m: response_text)
        msg = Message(id='m1', channel='stub', sender_id='u',
                      sender_name='U', chat_id='c1', text='hi')
        asyncio.run(registry._route_to_agent(msg))
        return adapter.sent

    def test_reply_with_marker_sends_media_and_clean_text(self):
        sent = self._run(f'Your receipt.\n[[MEDIA:{self.png}]]')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['text'], 'Your receipt.')
        self.assertEqual(len(sent[0]['media']), 1)
        self.assertEqual(os.path.realpath(sent[0]['media'][0].file_path),
                         os.path.realpath(self.png))

    def test_plain_reply_sends_no_media(self):
        sent = self._run('Just words')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['text'], 'Just words')
        self.assertIsNone(sent[0]['media'])


class _TextOnlyAdapter(_RecordingAdapter):
    async def send_message(self, chat_id, text, reply_to=None):
        self.sent.append({'chat_id': chat_id, 'text': text})
        return SendResult(success=True)


class _SendsThenTypeErrors(_RecordingAdapter):
    """Delivers, then trips over its own TypeError (e.g. building a result)."""

    async def send_message(self, chat_id, text, reply_to=None,
                           media=None, buttons=None):
        self.sent.append({'chat_id': chat_id, 'text': text, 'media': media})
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")


class TheReplyGoesOutOnce(unittest.TestCase):

    def _run(self, adapter, response_text='the answer'):
        registry = ChannelRegistry()
        registry._adapters['stub'] = adapter
        registry.set_agent_handler(lambda m: response_text)
        msg = Message(id='m1', channel='stub', sender_id='u',
                      sender_name='U', chat_id='c1', text='hi')
        asyncio.run(registry._route_to_agent(msg))
        return [s['text'] for s in adapter.sent]

    def test_an_adapter_without_media_gets_the_text(self):
        self.assertEqual(self._run(_TextOnlyAdapter()), ['the answer'])

    def test_an_adapters_own_typeerror_never_resends_the_reply(self):
        texts = self._run(_SendsThenTypeErrors())
        self.assertEqual(texts.count('the answer'), 1, texts)


class AFullPoolAnswersAtOnce(unittest.TestCase):

    def test_past_the_bound_a_message_is_not_queued(self):
        import threading
        from integrations.channels import registry as reg
        from core.constants import LLM_GENERIC_ERROR_REPLY
        slots = threading.BoundedSemaphore(1)
        slots.acquire()  # the one slot is busy
        ran = []
        registry = ChannelRegistry()
        adapter = _RecordingAdapter()
        registry._adapters['stub'] = adapter
        registry.set_agent_handler(lambda m: ran.append(m) or 'late answer')
        msg = Message(id='m1', channel='stub', sender_id='u',
                      sender_name='U', chat_id='c1', text='hi')
        with patch.object(reg, '_agent_handler_slots', slots):
            asyncio.run(registry._route_to_agent(msg))
        self.assertEqual(ran, [])
        self.assertEqual([s['text'] for s in adapter.sent],
                         [LLM_GENERIC_ERROR_REPLY])

    def test_the_slot_comes_back_when_the_turn_finishes(self):
        import threading
        from integrations.channels import registry as reg
        slots = threading.BoundedSemaphore(1)
        registry = ChannelRegistry()
        adapter = _RecordingAdapter()
        registry._adapters['stub'] = adapter
        registry.set_agent_handler(lambda m: 'ok')
        msg = Message(id='m1', channel='stub', sender_id='u',
                      sender_name='U', chat_id='c1', text='hi')
        with patch.object(reg, '_agent_handler_slots', slots):
            asyncio.run(registry._route_to_agent(msg))
            asyncio.run(registry._route_to_agent(msg))
        self.assertEqual([s['text'] for s in adapter.sent], ['ok', 'ok'])


if __name__ == '__main__':
    unittest.main()
