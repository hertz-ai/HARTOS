"""Unit tests for the optional Discord voice-receive bridge
(``integrations.channels.discord_voice_recv_sink``) — UNIF-G7 / W1.7
Producer A.

The discord-ext-voice-recv lib is NOT in the unit-test install set,
so HAS_VOICE_RECV is False on import.  These tests exercise the
production code paths that don't need the real lib (resampling,
maybe_attach_recv_sink fallback) and exercise the sink with
constructor-injected fakes for ``ws_connect`` + ``stt_port_provider``
so we don't need a real STT WS server either.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock


class ResampleTest(unittest.TestCase):
    """48kHz s16le stereo → 16kHz s16le mono — stdlib audioop only."""

    def test_empty_input_returns_empty(self):
        from integrations.channels.discord_voice_recv_sink import (
            _resample_48k_stereo_to_16k_mono,
        )
        self.assertEqual(_resample_48k_stereo_to_16k_mono(b''), b'')

    def test_known_silence_shrinks_by_3x(self):
        """48kHz stereo (2 ch × 2 bytes) → 16kHz mono (1 ch × 2 bytes):
        sample count drops 3× (rate) AND 2× (channels) — but tomono
        merges samples without changing per-sample count, so the
        ratio is 6× total: 1920 stereo bytes → 320 mono bytes."""
        from integrations.channels.discord_voice_recv_sink import (
            _resample_48k_stereo_to_16k_mono,
        )
        # 20ms of silence at 48kHz stereo s16le = 48000*0.02*2*2 = 3840 bytes
        silence = b'\x00\x00' * 1920
        out = _resample_48k_stereo_to_16k_mono(silence)
        # 20ms at 16kHz mono s16le = 16000*0.02*1*2 = 640 bytes
        self.assertEqual(len(out), 640)

    def test_garbage_returns_empty(self):
        """Odd-byte input violates s16 framing — audioop raises;
        the helper must swallow + return b'' (caller skips packet)."""
        from integrations.channels.discord_voice_recv_sink import (
            _resample_48k_stereo_to_16k_mono,
        )
        # 3 bytes — not a valid 16-bit stereo frame.
        self.assertEqual(_resample_48k_stereo_to_16k_mono(b'\x00\x01\x02'),
                         b'')


class HevolveStreamingSinkTest(unittest.TestCase):
    """Sink behavior with injected fakes — no real WS, no real lib."""

    def _make_fake_voice_data(self, pcm: bytes):
        return MagicMock(pcm=pcm)

    def _make_fake_user(self, uid: int):
        return MagicMock(id=uid)

    def _make_sink(self, call_id: str = 'call-VOICE-1',
                   bot_user_id: int = 999):
        from integrations.channels.discord_voice_recv_sink import (
            HevolveStreamingSink,
        )
        # Fake WS client — every connect returns a fresh MagicMock
        # whose .send / .close / iteration are all spies.
        ws_factories: list = []
        connect_calls: list = []

        def fake_connect(url, **kwargs):
            connect_calls.append(url)
            ws = MagicMock()
            ws.send = MagicMock()
            ws.close = MagicMock()
            ws.__iter__ = lambda self: iter([])
            ws_factories.append(ws)
            return ws

        sink = HevolveStreamingSink(
            call_id=call_id, bot_user_id=bot_user_id,
            ws_connect=fake_connect,
            stt_port_provider=lambda: 8005,
        )
        return sink, ws_factories, connect_calls

    def test_wants_opus_returns_false(self):
        sink, _, _ = self._make_sink()
        self.assertFalse(sink.wants_opus())

    def test_first_frame_opens_ws_per_speaker(self):
        sink, wss, urls = self._make_sink()
        # 20ms 48kHz stereo silence — non-empty after resample.
        silence = b'\x00\x00' * 1920
        sink.write(self._make_fake_user(1), self._make_fake_voice_data(silence))
        self.assertEqual(len(urls), 1)
        self.assertIn('call_id=call-VOICE-1', urls[0])
        self.assertIn('user_id=1', urls[0])
        self.assertEqual(wss[0].send.call_count, 1)

    def test_second_frame_same_speaker_reuses_ws(self):
        sink, wss, urls = self._make_sink()
        silence = b'\x00\x00' * 1920
        user = self._make_fake_user(1)
        sink.write(user, self._make_fake_voice_data(silence))
        sink.write(user, self._make_fake_voice_data(silence))
        # Same speaker → only ONE WS connect.
        self.assertEqual(len(urls), 1)
        self.assertEqual(wss[0].send.call_count, 2)

    def test_two_speakers_get_two_wss(self):
        sink, wss, urls = self._make_sink()
        silence = b'\x00\x00' * 1920
        sink.write(self._make_fake_user(1), self._make_fake_voice_data(silence))
        sink.write(self._make_fake_user(2), self._make_fake_voice_data(silence))
        self.assertEqual(len(urls), 2)
        self.assertIn('user_id=1', urls[0])
        self.assertIn('user_id=2', urls[1])

    def test_skips_bot_own_audio(self):
        sink, wss, urls = self._make_sink(bot_user_id=999)
        silence = b'\x00\x00' * 1920
        # The bot's own user_id — sink must NOT open a WS or send.
        sink.write(self._make_fake_user(999),
                   self._make_fake_voice_data(silence))
        self.assertEqual(len(urls), 0)
        self.assertEqual(len(wss), 0)

    def test_skips_empty_pcm(self):
        sink, wss, urls = self._make_sink()
        sink.write(self._make_fake_user(1),
                   self._make_fake_voice_data(b''))
        self.assertEqual(len(urls), 0)

    def test_skips_when_user_or_data_none(self):
        sink, wss, urls = self._make_sink()
        sink.write(None, MagicMock(pcm=b'\x00' * 100))
        sink.write(self._make_fake_user(1), None)
        self.assertEqual(len(urls), 0)

    def test_cleanup_closes_all_ws(self):
        sink, wss, urls = self._make_sink()
        silence = b'\x00\x00' * 1920
        sink.write(self._make_fake_user(1),
                   self._make_fake_voice_data(silence))
        sink.write(self._make_fake_user(2),
                   self._make_fake_voice_data(silence))
        sink.cleanup()
        for ws in wss:
            ws.close.assert_called_once()
        # After cleanup, further writes are no-ops.
        sink.write(self._make_fake_user(3),
                   self._make_fake_voice_data(silence))
        self.assertEqual(len(urls), 2)  # unchanged

    def test_send_failure_resets_ws(self):
        sink, wss, urls = self._make_sink()
        silence = b'\x00\x00' * 1920
        user = self._make_fake_user(1)
        sink.write(user, self._make_fake_voice_data(silence))
        # Make the FIRST WS error on send.
        wss[0].send = MagicMock(side_effect=RuntimeError('ws gone'))
        sink.write(user, self._make_fake_voice_data(silence))
        # Next write should reconnect (new WS).
        sink.write(user, self._make_fake_voice_data(silence))
        self.assertEqual(len(urls), 2)  # 2 connects total: original + reconnect


class MaybeAttachRecvSinkTest(unittest.TestCase):
    """``maybe_attach_recv_sink`` must be a no-op when HAS_VOICE_RECV
    is False (the unit-test install state) AND when the voice client
    doesn't support listen()."""

    def test_returns_false_when_lib_not_installed(self):
        # In this env discord-ext-voice-recv is NOT installed, so
        # HAS_VOICE_RECV is False.  Even with a perfectly valid
        # voice_client, the helper must short-circuit.
        from integrations.channels.discord_voice_recv_sink import (
            HAS_VOICE_RECV, maybe_attach_recv_sink,
        )
        self.assertFalse(HAS_VOICE_RECV)
        vc = MagicMock()
        vc.listen = MagicMock()
        ok = maybe_attach_recv_sink(vc, 'call-1', bot_user_id=42)
        self.assertFalse(ok)
        vc.listen.assert_not_called()

    def test_returns_false_when_voice_client_none(self):
        from integrations.channels.discord_voice_recv_sink import (
            maybe_attach_recv_sink,
        )
        self.assertFalse(maybe_attach_recv_sink(None, 'call-1'))

    def test_returns_false_when_voice_client_lacks_listen(self):
        # Forge HAS_VOICE_RECV=True for this test only — patch the
        # module-level flag.
        import integrations.channels.discord_voice_recv_sink as mod
        original = mod.HAS_VOICE_RECV
        mod.HAS_VOICE_RECV = True
        try:
            vc = MagicMock(spec=[])  # no listen() attribute
            self.assertFalse(mod.maybe_attach_recv_sink(vc, 'call-1'))
        finally:
            mod.HAS_VOICE_RECV = original


if __name__ == '__main__':
    unittest.main()
