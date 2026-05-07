"""Unit tests for the optional LiveKit RTC transcript subscriber
(``integrations.social.livekit_transcript_subscriber``) — UNIF-G7 /
W1.7 Producer B.

``livekit-rtc`` is NOT in the unit-test install set, so the module's
HAS_LIVEKIT_RTC is False on import.  These tests exercise the
production code paths that don't need the real lib (resampling,
push_frame, start/stop with no room) and use constructor-injected
fakes for the WS connector.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock


class ResampleTest(unittest.TestCase):

    def test_passthrough_when_already_16k_mono(self):
        from integrations.social.livekit_transcript_subscriber import (
            _resample_to_16k_mono,
        )
        # 20ms 16kHz mono s16le = 16000 * 0.02 * 1 * 2 = 640 bytes
        silence = b'\x00\x00' * 320
        out = _resample_to_16k_mono(silence, 16000, 1)
        # Passthrough — same bytes, same length.
        self.assertEqual(out, silence)

    def test_downmix_stereo_then_resample(self):
        from integrations.social.livekit_transcript_subscriber import (
            _resample_to_16k_mono,
        )
        # 20ms 48kHz stereo s16le = 48000 * 0.02 * 2 * 2 = 3840 bytes
        silence = b'\x00\x00' * 1920
        out = _resample_to_16k_mono(silence, 48000, 2)
        # → 16kHz mono s16le = 16000 * 0.02 * 1 * 2 = 640 bytes
        self.assertEqual(len(out), 640)

    def test_downsample_only_no_channel_change(self):
        from integrations.social.livekit_transcript_subscriber import (
            _resample_to_16k_mono,
        )
        # 20ms 48kHz mono s16le = 1920 bytes
        silence = b'\x00\x00' * 960
        out = _resample_to_16k_mono(silence, 48000, 1)
        # → 16kHz mono = 640 bytes
        self.assertEqual(len(out), 640)

    def test_empty_returns_empty(self):
        from integrations.social.livekit_transcript_subscriber import (
            _resample_to_16k_mono,
        )
        self.assertEqual(_resample_to_16k_mono(b'', 48000, 1), b'')

    def test_garbage_returns_empty(self):
        from integrations.social.livekit_transcript_subscriber import (
            _resample_to_16k_mono,
        )
        # 3 bytes — not a valid s16 frame.
        self.assertEqual(_resample_to_16k_mono(b'\x00\x01\x02', 48000, 1), b'')


class PushFrameTest(unittest.TestCase):
    """Producer B's primary public seam.  Exercised directly without
    a real LiveKit Room — drives the WS-forwarder side."""

    def _make_subscriber(self, call_id='call-LK-1'):
        from integrations.social.livekit_transcript_subscriber import (
            LiveKitTranscriptSubscriber,
        )
        ws_factories = []
        urls = []

        def fake_connect(url, **kwargs):
            urls.append(url)
            ws = MagicMock()
            ws.send = MagicMock()
            ws.close = MagicMock()
            ws.__iter__ = lambda self: iter([])
            ws_factories.append(ws)
            return ws

        sub = LiveKitTranscriptSubscriber(
            call_id=call_id,
            livekit_url='wss://livekit.example/test',
            token='fake-token',
            ws_connect=fake_connect,
            stt_port_provider=lambda: 8005,
        )
        return sub, ws_factories, urls

    def test_first_frame_opens_one_ws(self):
        sub, wss, urls = self._make_subscriber()
        # 20ms 48kHz mono silence
        silence = b'\x00\x00' * 960
        ok = sub.push_frame('alice', silence, src_rate=48000, src_channels=1)
        self.assertTrue(ok)
        self.assertEqual(len(urls), 1)
        self.assertIn('call_id=call-LK-1', urls[0])
        self.assertIn('user_id=alice', urls[0])
        wss[0].send.assert_called_once()

    def test_two_participants_two_ws(self):
        sub, wss, urls = self._make_subscriber()
        silence = b'\x00\x00' * 960
        sub.push_frame('alice', silence, 48000, 1)
        sub.push_frame('bob', silence, 48000, 1)
        self.assertEqual(len(urls), 2)
        self.assertIn('user_id=alice', urls[0])
        self.assertIn('user_id=bob', urls[1])

    def test_same_participant_reuses_ws(self):
        sub, wss, urls = self._make_subscriber()
        silence = b'\x00\x00' * 960
        sub.push_frame('alice', silence, 48000, 1)
        sub.push_frame('alice', silence, 48000, 1)
        self.assertEqual(len(urls), 1)
        self.assertEqual(wss[0].send.call_count, 2)

    def test_skips_when_call_id_missing(self):
        from integrations.social.livekit_transcript_subscriber import (
            LiveKitTranscriptSubscriber,
        )
        # Empty call_id — push_frame should not fire.
        sub = LiveKitTranscriptSubscriber(
            call_id='', livekit_url='wss://x', token='t')
        self.assertFalse(sub.push_frame('alice', b'\x00' * 100))

    def test_skips_empty_pcm(self):
        sub, wss, urls = self._make_subscriber()
        ok = sub.push_frame('alice', b'', 48000, 1)
        self.assertFalse(ok)
        self.assertEqual(len(urls), 0)

    def test_skips_after_stop(self):
        sub, wss, urls = self._make_subscriber()
        silence = b'\x00\x00' * 960
        sub.stop()  # idempotent without start
        ok = sub.push_frame('alice', silence, 48000, 1)
        self.assertFalse(ok)
        self.assertEqual(len(urls), 0)

    def test_send_failure_drops_ws_then_reconnects(self):
        sub, wss, urls = self._make_subscriber()
        silence = b'\x00\x00' * 960
        sub.push_frame('alice', silence, 48000, 1)
        wss[0].send = MagicMock(side_effect=RuntimeError('ws gone'))
        sub.push_frame('alice', silence, 48000, 1)
        # Next push should reconnect.
        sub.push_frame('alice', silence, 48000, 1)
        self.assertEqual(len(urls), 2)


class StartStopNoLibTest(unittest.TestCase):
    """When livekit-rtc is NOT installed AND no room_factory is given,
    start() must be a no-op and stop() must not raise."""

    def test_start_is_noop_without_lib(self):
        from integrations.social.livekit_transcript_subscriber import (
            LiveKitTranscriptSubscriber, HAS_LIVEKIT_RTC,
        )
        # In this env livekit-rtc is NOT installed.
        self.assertFalse(HAS_LIVEKIT_RTC)
        sub = LiveKitTranscriptSubscriber(
            call_id='c', livekit_url='wss://x', token='t')
        self.assertFalse(sub.start())
        # stop() is idempotent + safe.
        sub.stop()


class CrossProducerInvariantTest(unittest.TestCase):
    """Single-canonical-sink invariant: Producer B and Producer A
    funnel into the SAME enqueue point via the SAME local STT WS.
    """

    def test_url_uses_canonical_call_id_and_user_id_query(self):
        from integrations.social.livekit_transcript_subscriber import (
            LiveKitTranscriptSubscriber,
        )
        urls = []

        def fake_connect(url, **kwargs):
            urls.append(url)
            ws = MagicMock()
            ws.send = MagicMock()
            ws.__iter__ = lambda self: iter([])
            return ws

        sub = LiveKitTranscriptSubscriber(
            call_id='call-LK-7',
            livekit_url='wss://x', token='t',
            ws_connect=fake_connect,
            stt_port_provider=lambda: 8005,
        )
        sub.push_frame('participant-zed', b'\x00\x00' * 960, 48000, 1)
        # Same shape Producer A uses — Producer C parses both.
        self.assertEqual(len(urls), 1)
        self.assertIn('call_id=call-LK-7', urls[0])
        self.assertIn('user_id=participant-zed', urls[0])


if __name__ == '__main__':
    unittest.main()
