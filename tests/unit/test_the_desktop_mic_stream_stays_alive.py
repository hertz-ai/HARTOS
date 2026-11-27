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
  * the pause gate hears the room: speech at 1.5x or 2x a room of 300 or 800
    ends an utterance; a stream that opens on a fan finalizes no fan; a fan
    louder than the old line no longer holds every utterance open; the line
    drops back to 400 the moment the fan stops; a click is forgotten; speech
    in the first second is judged once the room is heard; under 400 is
    silence for the gate and the file gate alike;
  * a mic that stays open ({type:config, continuous:true}) ends an utterance
    at a pause, and a long quiet spell before it cannot get it cut in two by
    the 30 s flush, while anything louder than the room stays for the final;
    a call stream keeps its speech; a push-to-talk mic still never finalizes
    on a pause; when the page ends an utterance because the agent starts
    speaking (#289), a stream holding only the room decodes nothing, words
    said before the end are kept, and a push-to-talk release decodes as
    before;
  * the reader never waits on a transcription, finals still leave in the
    order spoken, and a final never lands before the interim still decoding;
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
ROOM, SOFT, FAN, VOICE = 30, 500, 600, 3000  # SOFT: over the 400 silence line
SOFT_OVER_FAN = 780  # a quiet voice over the fan: under its speech line (600 x 1.4)
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

    def test_speech_one_and_a_half_or_twice_the_room_ends_an_utterance(self):
        # rv_4594: the floor rose on every chunk, speech included, so speech
        # at 2x counted for one frame and 1.5x never; no final came at all.
        for room in (300, 800):
            for times in (1.5, 2.0):
                with self.subTest(room=room, times=times):
                    g = wt._StreamVadGate()
                    self.assertFalse(_feed(g, 2.0, room), 'the room alone ended an utterance')
                    self.assertFalse(_feed(g, 12.0, int(room * times)),
                                     'unbroken speech was cut: it lifted the line over itself')
                    self.assertTrue(_feed(g, 1.2, room), 'the pause after speech never ended it')

    def test_opening_on_a_fan_finalizes_no_fan(self):
        # rv_4594: in a room above 400 the fan counted as speech while the
        # floor climbed, and a final of pure fan came out at ~5 s.
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, 10.0, FAN), 'a final of nothing but the fan')
        self.assertGreater(g.speech_line(), FAN, 'the fan never became the floor')

    def test_a_fan_louder_than_the_old_line_does_not_hold_every_utterance_open(self):
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, 6.0, FAN))
        self.assertFalse(_feed(g, 1.0, VOICE))
        self.assertTrue(_feed(g, 1.2, FAN), 'the pause after speech, over the fan, never ended it')

    def test_a_voice_is_heard_the_moment_the_fan_stops(self):
        g = wt._StreamVadGate()
        _feed(g, 6.0, FAN)
        _feed(g, 1.0, ROOM)
        self.assertEqual(g.speech_line(), wt.STREAM_VAD_RMS_THRESHOLD)
        self.assertFalse(_feed(g, 1.0, SOFT))
        self.assertTrue(_feed(g, 1.2, ROOM))

    def test_a_click_shorter_than_a_page_frame_does_not_hold_the_stream_in_speech(self):
        # rv_4594: a click set "speech heard" without ever reaching an
        # utterance, so the quiet trim stayed off and the 30 s split was back.
        # A click shorter than any page frame (a container chunk) is forgotten.
        g = wt._StreamVadGate()
        _feed(g, 2.0, ROOM)
        self.assertFalse(g.update(VOICE, 100))
        self.assertTrue(g.heard_speech)
        self.assertFalse(_feed(g, 1.0, ROOM), 'a click is not an utterance')
        self.assertFalse(g.heard_speech, 'the click was never forgotten')

    def test_a_knock_in_one_page_frame_now_ends_an_utterance(self):
        # CHANGED by 987c37194, named here (rv_987c): with the minimum at
        # 250 ms, one 256 ms page frame is a word, and so is a knock that
        # lands in a frame's last 100 ms (the gate reads only that tail).
        # On 1c1792a2f (300 ms) it was forgotten like a click.  What the
        # person gets from it is the transcriber's: an empty or non-speech
        # final sends nothing (AMicThatStaysOpen pins that).
        g = wt._StreamVadGate()
        _feed(g, 2.0, ROOM)
        self.assertFalse(g.update(VOICE, FRAME_MS))
        self.assertTrue(_feed(g, 1.2, ROOM), 'a one-frame knock now ends an utterance')

    def test_a_one_frame_word_ends_an_utterance(self):
        # rv_1c17 on 1c1792a2f: one 256 ms page frame was under the 300 ms
        # minimum, so a child's "yes" or one drill syllable was forgotten like
        # a click and waited for the 30 s flush.
        g = wt._StreamVadGate()
        _feed(g, 2.0, ROOM)
        self.assertFalse(g.update(VOICE, FRAME_MS))
        self.assertTrue(_feed(g, 1.2, ROOM), 'a one-frame word got no final')

    def test_speech_from_the_moment_the_mic_opens_ends_an_utterance(self):
        # rv_1c17: a first second that was all speech became the floor (line
        # ~4200 for speech at 3000), so "hello" said as the mic opened was
        # never speech and waited for the 30 s flush: the owner's own
        # "hello do you hear me".
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, 1.5, VOICE))
        self.assertTrue(_feed(g, 1.2, ROOM), 'speech from the first moment got no final')

    def test_speech_over_a_fan_from_the_first_moment_is_judged_against_the_fan(self):
        # Both loud from the moment the mic opens: the warm-up runs to its
        # end with the fan as the floor, and the speech in it is then judged
        # against that floor and ends at the pause (hartos-17 on forward-2).
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, 1.5, VOICE))
        self.assertTrue(_feed(g, wt.STREAM_VAD_WARMUP_MAX_MS / 1000, FAN),
                        'speech over a fan from the first moment got no final')

    def test_unbroken_speech_past_the_warm_up_from_mic_open_gets_no_pause_final(self):
        # ACCEPTED, pinned (hartos-17 asked for a decision): speech that
        # starts as the mic opens and runs unbroken past the longest warm-up
        # is taken for the room, so it gets no pause final of its own.  At the
        # handler it rides into the NEXT utterance's final (one long final, two
        # utterances in one chat turn; rv_987c), as on the parent.  The page
        # opens its mic when it loads, so a drill
        # normally starts long after the room was heard; and natural speech
        # has quiet frames (the gate reads each frame's last 100 ms) that end
        # the warm-up sooner.  Changing this needs re-judging after the
        # warm-up, which this forward does not do.
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, wt.STREAM_VAD_WARMUP_MAX_MS / 1000 + 2, VOICE))
        self.assertFalse(_feed(g, 1.2, ROOM))

    def test_a_loud_room_still_becomes_the_floor_when_it_never_goes_quiet(self):
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, wt.STREAM_VAD_WARMUP_MAX_MS / 1000 + 1, FAN),
                         'a fan from the first moment was finalized')
        self.assertGreater(g.speech_line(), FAN)

    def test_the_gate_and_the_file_gate_share_one_silence_line(self):
        # rv_4594: the gate went down to 120 while _audio_is_silent still
        # dropped anything under 400, so what the gate heard the worker threw
        # away.  One line now: under it is silence for both, over it is not.
        import tempfile
        import wave

        def _wav(level):
            path = os.path.join(tempfile.mkdtemp(), 'a.wav')
            with wave.open(path, 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(_audio(2.0, level))
            return path

        under, over = wt.STREAM_VAD_RMS_THRESHOLD - 100, wt.STREAM_VAD_RMS_THRESHOLD + 100
        for level, silent in ((under, True), (over, False)):
            with self.subTest(level=level):
                self.assertIs(wt._audio_is_silent(_wav(level)), silent)
                g = wt._StreamVadGate()
                _feed(g, 2.0, ROOM)
                _feed(g, 1.0, level)
                self.assertEqual(g.heard_speech, not silent)

    def test_speech_in_the_first_second_is_judged_once_the_room_is_heard(self):
        # The warm-up ended only on a chunk landing exactly on 1000 ms; the
        # page's 256 ms frames never do, so what was said as the mic opened
        # was never counted and a short "hello" there got no final.
        g = wt._StreamVadGate()
        self.assertFalse(_feed(g, FRAME_MS / 1000, ROOM))
        self.assertFalse(_feed(g, 3 * FRAME_MS / 1000, VOICE))
        self.assertTrue(_feed(g, 1.2, ROOM), 'speech said as the mic opened was lost')


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

    def test_hello_said_as_the_mic_opens_gets_its_final(self):
        # The owner's "hello do you hear me", said the moment the mic opened.
        socket = self._speak(CONTINUOUS, *_frames(1.5, VOICE), *_frames(1.2, ROOM))
        self.assertEqual(socket.finals(), [SAID])

    def test_a_one_frame_word_gets_its_final(self):
        socket = self._speak(CONTINUOUS, *_frames(2.0, ROOM),
                             *_frames(FRAME_MS / 1000, VOICE), *_frames(1.2, ROOM))
        self.assertEqual(socket.finals(), [SAID])

    def test_a_page_frame_knock_the_transcriber_hears_as_nothing_sends_nothing(self):
        # rv_987c: since 987c37194 a knock in one page frame ends an
        # utterance.  Whisper (faster-whisper, first in the engine chain, with
        # its own speech filter) returns no words for it, or a non-speech tag;
        # either final sends the person nothing.
        for heard in ('', '(knocking)', '[BLANK_AUDIO]'):
            socket = self._speak(CONTINUOUS, *_frames(2.0, ROOM),
                                 *_frames(FRAME_MS / 1000, VOICE), *_frames(1.2, ROOM),
                                 transcribe=_stand_in(final_text=heard))
            self.assertEqual([m for m in socket.sent if m.get('is_final')], [], heard)

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

    def test_a_mic_that_opens_on_a_fan_finalizes_no_fan(self):
        # rv_4594: in a room above 400 a final of pure fan came out at ~5 s.
        self.assertEqual(self._speak(CONTINUOUS, *_frames(10.0, FAN)).finals(), [])
        socket = self._speak(CONTINUOUS, *_frames(3.0, FAN), *_frames(1.5, VOICE),
                             *_frames(1.2, FAN))
        self.assertEqual(socket.finals(), [SAID])

    def test_a_voice_under_the_speech_line_is_kept_for_the_final(self):
        # rv_4594: while nobody "spoke" the quiet trim dropped everything
        # under the speech line, so a soft voice just under it (410 in a room
        # of 300, line 420) was thrown away.  Only the room itself goes now.
        soft = []
        for _ in range(12):
            soft += _frames(2 * FRAME_MS / 1000, 410) + _frames(FRAME_MS / 1000, 300)
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(9.0, 300), *soft, FINAL,
                             transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(socket.finals(), [SAID])
        self.assertGreaterEqual(sizes[0], sum(len(f) for f in soft),
                                'the soft voice was trimmed away')

    def test_an_end_from_the_page_with_only_the_room_held_decodes_nothing(self):
        # #289 rv_2e86 F2: the chat page ends the open utterance each time
        # the agent starts speaking ({control:final}, Nunba 2e866857).  When
        # all the stream holds is the room, every chunk near its floor, that
        # audio is dropped.  A quiet room, or a fan at RMS 600 that has
        # become the floor, was decoded at every voice start, and the fan
        # passes the 400 silence gate into the model's own filters.
        for level in (ROOM, FAN):
            with self.subTest(room=level):
                sizes = []
                socket = self._speak(CONTINUOUS, *_frames(8.0, level), FINAL,
                                     *_frames(1.2, level),
                                     transcribe=_stand_in(final_sizes=sizes))
                self.assertEqual(sizes, [], 'the room was sent to be decoded')
                self.assertEqual(socket.finals(), [])

    def test_an_end_from_the_page_keeps_what_was_said_before_it(self):
        # The person was talking when the agent started: the words so far
        # are their utterance, ended there, not dropped.
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(2.0, ROOM), *_frames(0.6, VOICE), FINAL,
                             transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(socket.finals(), [SAID])
        self.assertGreaterEqual(sizes[0], len(_audio(0.6, VOICE)), 'the words were cut')

    def test_a_push_to_talk_release_still_decodes_what_it_holds(self):
        # A clicked mic keeps no room levels: its Stop decodes as before.
        sizes = []
        self._speak(*_frames(2.0, ROOM), FINAL, transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(len(sizes), 1)

    def test_a_syllable_early_in_a_frame_is_not_taken_for_the_room(self):
        # hartos-17 on 0c7673852: a held chunk's level was its last 100 ms
        # (_pcm_rms), so a word in the first ~156 ms of a 256 ms page frame,
        # the rest of it quiet, read as the room, and the end dropped it.
        # Every 100 ms window of the chunk counts now (_pcm_peak_rms, the
        # silence gate's own measure).
        head = int(RATE * 0.15)
        syllable = _audio(head / RATE, VOICE) + _audio((FRAME - head) / RATE, ROOM)
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(2.0, ROOM), syllable, *_frames(0.5, ROOM),
                             FINAL, transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(len(sizes), 1, 'the syllable was dropped as the room')
        self.assertEqual(socket.finals(), [SAID])

    def test_a_soft_voice_over_a_fan_is_not_taken_for_the_room(self):
        # hartos-17 on 0c7673852 (probe A4): a voice at 780 over a fan at
        # 600 is under the speech line (840), so the floor climbs toward it
        # (at most 2x per 4 s).  Judged by the floor at the end, the voice
        # read as the room after ~1.5 s and was dropped: the quiet child's
        # voice.  It is judged by the room's floor from before it.
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(6.0, FAN), *_frames(3.0, SOFT_OVER_FAN),
                             FINAL, transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(len(sizes), 1, 'the soft voice was dropped as the room')
        self.assertEqual(socket.finals(), [SAID])

    def test_a_knock_early_in_a_frame_never_holds_the_quiet_trim(self):
        # hartos-17 on 861465ab5 (probe T1, T0 the control): the quiet trim
        # judged each chunk by its loudest 100 ms, so a knock early in one
        # frame, which the pause gate never hears, stopped the trim at the
        # front.  The buffer grew to the 30 s flush, and ~30 s of room plus
        # the knock went to the model.  The trim judges the room as the
        # pause gate hears it, so the knock goes with the room.
        head = int(RATE * 0.1)
        knock = _audio(head / RATE, VOICE) + _audio((FRAME - head) / RATE, ROOM)
        sizes = []
        socket = self._speak(CONTINUOUS, *_frames(2.0, ROOM), knock, *_frames(32.0, ROOM),
                             transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(sizes, [], 'the room was flushed to the model')
        self.assertEqual(socket.finals(), [])

    def test_a_call_keeps_its_speech(self):
        # A call ends its utterances at pauses too; the quiet trim and the
        # room's line must leave a caller at 1.5x the room whole.
        sizes, segments = [], []
        with mock.patch.object(wt, '_maybe_enqueue_call_segment',
                               lambda *a: segments.append(a)):
            socket = self._speak(*_frames(9.0, 300), *_frames(1.5, 450),
                                 *_frames(1.2, 300), path='/?call_id=7&user_id=42',
                                 transcribe=_stand_in(final_sizes=sizes))
        self.assertEqual(socket.finals(), [SAID])
        self.assertEqual([a[2] for a in segments], [SAID])
        self.assertGreaterEqual(sizes[0], len(_audio(1.5, 450)), 'the caller was cut')

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

    def test_a_final_never_lands_before_the_interim_still_decoding(self):
        # rv_4594: a slow interim sent after its final put stale words back
        # on the page under the finished sentence.
        def _transcribe(buf, keep_buffer=False, language=None):
            if keep_buffer:
                time.sleep(0.4)
                return ('hel', 'en')
            return (SAID, 'en')

        socket = self._speak(*_frames(2.0, VOICE), FINAL, transcribe=_transcribe)
        kinds = [m.get('is_final') for m in socket.sent]
        self.assertIn(False, kinds, 'no interim ran')
        self.assertEqual(kinds[-1], True, socket.sent)

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
