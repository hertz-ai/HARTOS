"""Unit tests for the streaming-STT realtime window bound + GPU device
selection in ``integrations.service_tools.whisper_tool``.

Why these tests exist
---------------------
Live symptom: the streaming STT WS server (port 8005) responded but was
VERY DELAYED — latency grew the longer the user spoke.  Root cause: the
INTERIM transcription path re-decoded the ENTIRE accumulated audio buffer
every ``STREAM_CHUNK_SECONDS`` (O(n²) over an utterance).  The fix bounds
the interim re-decode to the most-recent ``STREAM_INTERIM_WINDOW_SECONDS``
of audio while the FINAL pass (control 'final' + MAX_BUFFER force-flush)
still decodes the full utterance for accuracy.

A secondary fix makes the device-selection log a clear WARNING (naming the
reason) when ctranslate2 CUDA is unavailable, so an operator on a CUDA box
knows the GPU isn't engaged.

These tests mock the model + the subprocess transcribe entirely — they do
NOT require a real GPU or real audio.
"""
from __future__ import annotations

import asyncio
import io
import sys
import types
import unittest
from unittest import mock

from integrations.service_tools import whisper_tool


def _pcm(n_bytes: int) -> bytes:
    """A blob of raw PCM16 bytes of the requested length (content irrelevant)."""
    return b"\x01\x02" * (n_bytes // 2)


class _FakeWS:
    """Minimal async-iterable websocket double for ``_stt_stream_handler``.

    Yields a scripted list of messages, records every ``send`` payload, and
    exposes an empty ``request.path`` (no ``?call_id=``) so the call-segment
    enqueue helper degrades to a no-op.
    """

    class _Req:
        path = "/"

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []
        self.request = self._Req()

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()

    async def send(self, payload):
        self.sent.append(payload)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class InterimWindowBoundTest(unittest.TestCase):
    """Part A — interim transcription is invoked on a BOUNDED slice."""

    def test_tail_window_buffer_caps_length_and_aligns(self):
        # Source buffer larger than the window → tail is exactly the window
        # size (already even) and is the LAST bytes, not the first.
        full = bytes(range(256)) * 100  # 25600 bytes
        src = io.BytesIO(full)
        src.seek(123)  # arbitrary position — must be read non-destructively
        window = 6000
        out = whisper_tool._tail_window_buffer(src, window)
        tail = out.getvalue()
        self.assertEqual(len(tail), window)
        self.assertEqual(tail, full[-window:])
        # Source untouched (position + contents preserved).
        self.assertEqual(src.getvalue(), full)
        self.assertEqual(src.tell(), 123)

    def test_tail_window_buffer_odd_window_aligns_down_to_sample(self):
        full = _pcm(20000)
        out = whisper_tool._tail_window_buffer(full and io.BytesIO(full), 5001)
        # 5001 is odd; start = 20000-5001 = 14999 → aligned down to 14998
        # (even), so tail length = 20000-14998 = 5002 (even, whole samples).
        self.assertEqual(len(out.getvalue()) % whisper_tool.STREAM_BYTES_PER_SAMPLE, 0)

    def test_tail_window_buffer_short_buffer_returns_all(self):
        full = _pcm(2000)
        out = whisper_tool._tail_window_buffer(io.BytesIO(full), 6000)
        self.assertEqual(out.getvalue(), full)

    def test_interim_decode_is_bounded_while_buffer_is_larger(self):
        """Drive the handler with > window seconds of audio and assert every
        INTERIM call to ``_transcribe_buffer`` sees <= window bytes, even
        though the accumulated buffer is much larger."""
        window = whisper_tool.STREAM_INTERIM_WINDOW_BYTES
        chunk = whisper_tool.STREAM_CHUNK_BYTES

        # Feed ~10 chunks of 2s each → ~20s total, far exceeding the 6s
        # interim window, but below the 30s MAX_BUFFER force-flush.
        n_chunks = 10
        messages = [_pcm(chunk) for _ in range(n_chunks)]
        ws = _FakeWS(messages)

        seen_interim_sizes = []

        def _fake_transcribe(buf, keep_buffer=False):
            # keep_buffer=True is the interim path; record its byte length.
            if keep_buffer:
                seen_interim_sizes.append(len(buf.getvalue()))
            return ("partial text", "en")

        with mock.patch.object(whisper_tool, "_transcribe_buffer", _fake_transcribe):
            _run(whisper_tool._stt_stream_handler(ws))

        self.assertTrue(seen_interim_sizes, "no interim transcription happened")
        # Total accumulated audio far exceeds the window → had it been
        # unbounded, sizes would climb past `window`.  With the fix, every
        # interim decode is capped at the window (+/- one sample alignment).
        for sz in seen_interim_sizes:
            self.assertLessEqual(
                sz, window + whisper_tool.STREAM_BYTES_PER_SAMPLE,
                f"interim decode size {sz} exceeded window {window}")
        # And the buffer genuinely grew past the window (otherwise the bound
        # would be vacuously true).
        self.assertGreater(n_chunks * chunk, window)

    def test_final_control_decodes_full_buffer(self):
        """Part A — the 'final' control still transcribes the FULL buffer."""
        chunk = whisper_tool.STREAM_CHUNK_BYTES
        # 4 chunks accumulated, then a 'final' control message.
        import json as _json
        messages = [_pcm(chunk) for _ in range(4)] + [
            _json.dumps({"control": "final"})]
        ws = _FakeWS(messages)

        final_sizes = []
        interim_sizes = []

        def _fake_transcribe(buf, keep_buffer=False):
            if keep_buffer:
                interim_sizes.append(len(buf.getvalue()))
            else:
                final_sizes.append(len(buf.getvalue()))
            return ("done", "en")

        with mock.patch.object(whisper_tool, "_transcribe_buffer", _fake_transcribe):
            _run(whisper_tool._stt_stream_handler(ws))

        self.assertEqual(len(final_sizes), 1, "expected exactly one final decode")
        # The final decode must see the FULL accumulated buffer (4 chunks),
        # not just the interim window.
        self.assertGreaterEqual(final_sizes[0], 4 * chunk - chunk)  # ~full
        self.assertGreater(final_sizes[0], whisper_tool.STREAM_INTERIM_WINDOW_BYTES)
        # A final is_final=True frame was sent.
        self.assertTrue(any('"is_final": true' in s for s in ws.sent))

    def test_max_buffer_force_flush_decodes_full_buffer(self):
        """Part A — the MAX_BUFFER force-flush is a FINAL decode (keep_buffer
        False) over the whole buffer, not a windowed interim."""
        # One frame at/over the 30s max → triggers force-flush before interim.
        big = _pcm(whisper_tool.STREAM_MAX_BUFFER_BYTES)
        ws = _FakeWS([big])

        final_sizes = []

        def _fake_transcribe(buf, keep_buffer=False):
            if not keep_buffer:
                final_sizes.append(len(buf.getvalue()))
            return ("flushed", "en")

        with mock.patch.object(whisper_tool, "_transcribe_buffer", _fake_transcribe):
            _run(whisper_tool._stt_stream_handler(ws))

        self.assertEqual(len(final_sizes), 1)
        self.assertGreaterEqual(final_sizes[0], whisper_tool.STREAM_MAX_BUFFER_BYTES)


class DeviceSelectionFallbackTest(unittest.TestCase):
    """Part B — device selection falls back to CPU without raising when
    ctranslate2 CUDA is absent, and emits a clear WARNING naming the reason."""

    def setUp(self):
        # Reset the module-level model cache so the loader runs fresh.
        self._saved_model = whisper_tool._faster_whisper_model
        self._saved_size = whisper_tool._faster_whisper_model_size
        whisper_tool._faster_whisper_model = None
        whisper_tool._faster_whisper_model_size = None
        # Reset breaker/backoff so they don't short-circuit the load.
        whisper_tool._whisper_load_breaker = None
        whisper_tool._whisper_load_backoff = None

    def tearDown(self):
        whisper_tool._faster_whisper_model = self._saved_model
        whisper_tool._faster_whisper_model_size = self._saved_size

    def _install_fake_modules(self, cuda_supported):
        """Inject fake faster_whisper + ctranslate2 into sys.modules.

        ``cuda_supported``: bool | 'raise' — controls what
        ctranslate2.get_supported_compute_types('cuda') does.
        """
        captured = {}

        fake_fw = types.ModuleType("faster_whisper")

        class _FakeWhisperModel:
            def __init__(self, model_size, device="cpu", compute_type="int8"):
                captured["device"] = device
                captured["compute_type"] = compute_type
        fake_fw.WhisperModel = _FakeWhisperModel

        fake_ct = types.ModuleType("ctranslate2")

        def _gsct(name):
            if cuda_supported == "raise":
                raise RuntimeError("broken CUDA runtime")
            return ("int8", "float32") + (("cuda",) if cuda_supported else ())
        fake_ct.get_supported_compute_types = _gsct

        return fake_fw, fake_ct, captured

    def test_cpu_fallback_when_no_cuda(self):
        fake_fw, fake_ct, captured = self._install_fake_modules(cuda_supported=False)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw,
                                           "ctranslate2": fake_ct}):
            with self.assertLogs(whisper_tool.logger, level="WARNING") as cm:
                model = whisper_tool._get_faster_whisper_model("base")
        self.assertIsNotNone(model)
        self.assertEqual(captured["device"], "cpu")
        self.assertEqual(captured["compute_type"], "int8")
        # A clear WARNING naming WHY we fell back (operator signal).
        joined = "\n".join(cm.output)
        self.assertIn("CUDA not available", joined)
        self.assertIn("CPU", joined)

    def test_cpu_fallback_when_ctranslate2_probe_raises(self):
        fake_fw, fake_ct, captured = self._install_fake_modules(cuda_supported="raise")
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw,
                                           "ctranslate2": fake_ct}):
            # Must NOT raise — broken CUDA runtime degrades to CPU.
            with self.assertLogs(whisper_tool.logger, level="WARNING"):
                model = whisper_tool._get_faster_whisper_model("base")
        self.assertIsNotNone(model)
        self.assertEqual(captured["device"], "cpu")

    def test_cuda_selected_when_available(self):
        fake_fw, fake_ct, captured = self._install_fake_modules(cuda_supported=True)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw,
                                           "ctranslate2": fake_ct}):
            model = whisper_tool._get_faster_whisper_model("base")
        self.assertIsNotNone(model)
        self.assertEqual(captured["device"], "cuda")
        self.assertEqual(captured["compute_type"], "float16")


if __name__ == "__main__":
    unittest.main()
