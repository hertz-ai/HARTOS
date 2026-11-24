"""The desktop mic keeps hearing the person (owner 2026-11-24).

What happened on the installed Nunba that day: the owner said "hello do you
hear me" and nothing showed.  The speech stream (ws://127.0.0.1:8005) had
received audio in bursts and then closed with only a DEBUG line; a mic the page
opens by itself is never released, so its only final was the 30 s flush; the
pause gate compared every room against a fixed RMS line of 400; and nothing the
desktop mic heard reached the person's voice window.  The owner: "it shd be
adaptive based on voice activity and energy", "it should all be natural and
intuitive for user".

Pinned here, through the real stream handler with a scripted socket and a
stand-in for the Whisper worker:
  * the pause gate hears the room: a quiet child in a quiet room ends an
    utterance; a fan louder than the old line no longer holds every utterance
    open; the line drops back the moment the fan stops;
  * a mic that stays open ({type:config, continuous:true}) ends an utterance
    at a pause, and a long quiet spell before it cannot get it cut in two by
    the 30 s flush; a push-to-talk mic still never finalizes on a pause;
  * the reader never waits on a transcription, and finals still leave in the
    order spoken;
  * the stream's end is logged at INFO with its close code;
  * a final heard on a signed-in desktop mic is told apart for the voice
    window from the in-memory audio (a call's speakers are left to the
    diarizer), and the voice reader takes those bytes without a file.
"""
import array
import asyncio
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.service_tools import whisper_tool as wt  # noqa: E402

RATE = wt.STREAM_SAMPLE_RATE
FRAME = 4096                      # samples per page frame (ScriptProcessor 4096)
FRAME_MS = FRAME * 1000.0 / RATE  # 256 ms
ROOM, CHILD, FAN, VOICE = 30, 200, 600, 3000
SAID = 'hello do you hear me'


def _audio(seconds, level):
    """PCM16 mono at 16 kHz whose RMS is ``level``."""
    n = int(RATE * seconds)
    return array.array('h', [level if i % 2 else -level for i in range(n)]).tobytes()


def _frames(seconds, level):
    data = _audio(seconds, level)
    step = FRAME * 2
    return [data[i:i + step] for i in range(0, len(data), step)]


def _feed(gate, seconds, level):
    """Feed a gate ``seconds`` of one level, frame by frame; did it fire?"""
    return any([gate.update(level, FRAME_MS)
                for _ in range(int(round(seconds * 1000 / FRAME_MS)))])


class _Socket:
    """The page's socket: yields to the loop between frames, as a real one
    does, counts what the server has read, and records what it sent."""

    def __init__(self, messages, path='/', close_code=None, close_reason=''):
        self._messages = list(messages)
        self.request = types.SimpleNamespace(path=path)
        self.close_code = close_code
        self.close_reason = close_reason
        self.consumed = 0
        self.sent = []

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                await asyncio.sleep(0)
                self.consumed += 1
                yield m
        return _gen()

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def finals(self):
        return [m['text'] for m in self.sent if m.get('is_final')]


def _serve(socket):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(wt._stt_stream_handler(socket))
    finally:
        loop.close()


def _stand_in(final_text=SAID, interim_text='hel', final_sizes=None):
    def _transcribe(buf, keep_buffer=False, language=None):
        if keep_buffer:
            return (interim_text, 'en')
        if final_sizes is not None:
            final_sizes.append(len(buf.getvalue()))
        return (final_text, 'en')
    return _transcribe


CONTINUOUS = json.dumps({'type': 'config', 'continuous': True})
FINAL = json.dumps({'control': 'final'})


class TheRoomSetsTheSpeechLine(unittest.TestCase):

    def test_before_the_room_is_heard_the_line_is_the_old_one(self):
        self.assertEqual(wt._StreamVadGate().speech_line(), wt.STREAM_VAD_RMS_THRESHOLD)

    def test_a_quiet_child_in_a_quiet_room_ends_an_utterance(self):
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, 2.0, ROOM))
        self.assertFalse(_feed(g, 1.0, CHILD))
        self.assertTrue(_feed(g, 1.2, ROOM), 'a child at RMS 200 was never heard')

    def test_a_fan_louder_than_the_old_line_does_not_hold_every_utterance_open(self):
        g = wt._StreamVadGate()
        _feed(g, 6.0, FAN)
        self.assertGreater(g.speech_line(), FAN, 'the fan never became the floor')
        self.assertFalse(_feed(g, 1.0, VOICE))
        self.assertTrue(_feed(g, 1.2, FAN), 'the pause after speech, over the fan, never ended it')

    def test_a_soft_voice_is_heard_the_moment_the_fan_stops(self):
        g = wt._StreamVadGate()
        _feed(g, 6.0, FAN)
        _feed(g, 1.0, ROOM)
        self.assertFalse(_feed(g, 1.0, CHILD))
        self.assertTrue(_feed(g, 1.2, ROOM))


class AMicThatStaysOpen(unittest.TestCase):

    def setUp(self):
        # The learner producer reads the consent DB and posts to HevolveAI;
        # what it receives is pinned in test_whisper_stream_call_id.py.
        p = mock.patch.object(wt, '_maybe_ingest_audio_sensor')
        p.start()
        self.addCleanup(p.stop)

    def _speak(self, *messages, path='/', transcribe=None):
        socket = _Socket(messages, path=path)
        with mock.patch.object(wt, '_transcribe_buffer', transcribe or _stand_in()):
            _serve(socket)
        return socket

    def test_an_open_mic_ends_an_utterance_at_a_pause(self):
        socket = self._speak(CONTINUOUS, *_frames(1.0, ROOM), *_frames(1.5, VOICE),
                             *_frames(1.2, ROOM))
        self.assertEqual(socket.finals(), [SAID])

    def test_a_push_to_talk_mic_still_never_finalizes_on_a_pause(self):
        socket = self._speak(*_frames(1.0, ROOM), *_frames(1.5, VOICE), *_frames(1.2, ROOM))
        self.assertEqual(socket.finals(), [])

    def test_a_long_quiet_spell_does_not_get_the_next_utterance_cut_in_two(self):
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(25.0, ROOM), *_frames(8.0, VOICE),
                             *_frames(1.2, ROOM), transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(socket.finals(), [SAID])
        voice_bytes = len(_audio(8.0, VOICE))
        self.assertGreaterEqual(sizes[0], voice_bytes, 'the utterance was cut')
        self.assertLess(sizes[0], wt.STREAM_MAX_BUFFER_BYTES,
                        'the quiet spell rode in front of the utterance')

    def test_reading_never_waits_on_a_slow_transcription(self):
        read_when_done = []
        socket_box = []

        def _slow(buf, keep_buffer=False, language=None):
            time.sleep(0.4)
            read_when_done.append(socket_box[0].consumed)
            return ('hel', 'en')

        frames = _frames(6.0, VOICE)
        socket = _Socket(frames)
        socket_box.append(socket)
        with mock.patch.object(wt, '_transcribe_buffer', _slow):
            _serve(socket)
        self.assertTrue(read_when_done, 'no transcription ran')
        self.assertEqual(read_when_done[0], len(frames),
                         'the reader stopped while Whisper was working')

    def test_finals_leave_in_the_order_they_were_spoken(self):
        lock, calls = threading.Lock(), []

        def _transcribe(buf, keep_buffer=False, language=None):
            if keep_buffer:
                return ('', 'en')
            with lock:
                calls.append(1)
                first = len(calls) == 1
            if first:
                time.sleep(0.3)
            return ('first' if first else 'second', 'en')

        socket = self._speak(*_frames(1.5, VOICE), FINAL, *_frames(1.5, VOICE), FINAL,
                             transcribe=_transcribe)
        self.assertEqual(socket.finals(), ['first', 'second'])

    def test_the_end_of_a_stream_is_logged_with_its_close_code(self):
        socket = _Socket(_frames(2.0, VOICE), close_code=1011,
                         close_reason='keepalive ping timeout')
        with mock.patch.object(wt, '_transcribe_buffer', _stand_in()), \
                self.assertLogs(wt.logger, level='INFO') as seen:
            _serve(socket)
        ended = [r for r in seen.output if 'STT stream ended' in r]
        self.assertEqual(len(ended), 1, seen.output)
        self.assertIn('1011', ended[0])
        self.assertIn('keepalive ping timeout', ended[0])
        self.assertIn('2.0 s of audio received', ended[0])


class TheDesktopMicReachesTheVoiceWindow(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(wt, '_maybe_ingest_audio_sensor')
        p.start()
        self.addCleanup(p.stop)

    def _heard(self, path):
        heard = mock.Mock()
        utterance = _audio(2.0, VOICE)
        socket = _Socket([utterance, FINAL], path=path)
        with mock.patch.object(wt, '_transcribe_buffer', _stand_in()), \
                mock.patch.object(wt, '_hear_voice', heard):
            _serve(socket)
            deadline = time.time() + 2.0
            while not heard.called and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
        return heard, utterance

    def test_a_final_on_a_signed_in_mic_is_told_apart_once(self):
        heard, utterance = self._heard('/?user_id=42')
        heard.assert_called_once_with('42', utterance, SAID)

    def test_a_call_leaves_its_speakers_to_the_diarizer(self):
        heard, _ = self._heard('/?call_id=7&user_id=42')
        heard.assert_not_called()

    def test_the_voice_reader_takes_live_mic_bytes_without_a_file(self):
        from integrations.audio import voiceprint
        pcm = array.array('h', [16384, -16384, 0]).tobytes() + b'\x01'
        self.assertEqual(list(voiceprint._read_audio(pcm)), [0.5, -0.5, 0.0])


if __name__ == '__main__':
    unittest.main()
