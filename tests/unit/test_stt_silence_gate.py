"""Silent audio must not become a chat message.

Whisper fabricates fluent, grammatical prose from silence and emits it with
CONFIDENT scores.  Live 2026-08-17, conv 730fa326 (media_mode=audio,
casual=True) — every one of these reached chat and was answered:

    "I'm really happy with the reaction."
    "I don't think I can do anything."
    "I think I'm going to be a little bit more important."
    "I don't think we're here."

Nobody said them.  Measuring the real STT temp inputs from that period against
the existing STREAM_VAD_RMS_THRESHOLD (400):

    tmpq7suc3wp.wav   6.00s  RMS   86   <- 6 seconds of near-silence
    15 others         0.00s  RMS    0
    16/16 below the threshold

The two gates already in whisper_tool cannot catch this:
  * _filter_speech_text keys on no_speech_prob / avg_logprob, and these
    hallucinations score as confident speech.
  * _drop_non_speech_text keys on annotation SHAPE — brackets, music notes,
    repeated spans.  Fluent prose has none.
Both ask "does this look like a caption?".  The question that separates these
cases is "was anyone speaking?", which is an energy question.

Diarization would also answer it, but whisperx and pyannote.audio are not
installed, so DiarizationService._is_whisperx_available() is False and the
service never starts.  A gate built on it could not fire.

The sparse-speech tests below are the ones that matter for regressions: a gate
that drops real quiet speech is far worse than the bug it fixes.
"""
import array
import math
import struct
import tempfile
import wave

import pytest

from integrations.service_tools import whisper_tool as wt


def _wav(path, samples, rate=16000):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array('h', samples).tobytes())


def _tone(n, amp):
    return [int(amp * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]


# ── peak windowed RMS ────────────────────────────────────────────────────────

def test_peak_rms_finds_speech_in_a_mostly_silent_file():
    """THE REGRESSION GUARD: 2s of speech inside 30s of silence is SPEECH.

    Overall RMS would average this down to silence and discard a real
    utterance.  _pcm_rms is also wrong here -- it reads only the last ~100ms,
    so speech that ends quietly would read as silence.
    """
    pcm = array.array('h', [0] * (16000 * 28) + _tone(16000 * 2, 6000)).tobytes()
    peak = wt._pcm_peak_rms(pcm)
    assert peak > wt.STREAM_VAD_RMS_THRESHOLD, (
        f"peak RMS {peak:.0f} — 2s of real speech in a long file was scored as "
        f"silence; this would drop genuine user utterances")


def test_peak_rms_speech_at_the_very_start_survives():
    """Speech first, then a long silent tail — the tail-capped _pcm_rms would
    miss this entirely."""
    pcm = array.array('h', _tone(16000 * 2, 6000) + [0] * (16000 * 20)).tobytes()
    assert wt._pcm_peak_rms(pcm) > wt.STREAM_VAD_RMS_THRESHOLD


def test_peak_rms_true_silence_is_silence():
    pcm = array.array('h', [0] * 16000 * 6).tobytes()
    assert wt._pcm_peak_rms(pcm) < wt.STREAM_VAD_RMS_THRESHOLD


def test_peak_rms_low_level_noise_is_silence():
    """The live case: 6s at RMS ~86 (well under 400) produced a fluent
    sentence."""
    pcm = array.array('h', [80 if i % 2 else -80 for i in range(16000 * 6)]).tobytes()
    peak = wt._pcm_peak_rms(pcm)
    assert peak < wt.STREAM_VAD_RMS_THRESHOLD, f"peak {peak:.0f} should be silence"


def test_peak_rms_empty_and_odd_length_are_safe():
    assert wt._pcm_peak_rms(b'') == 0.0
    assert wt._pcm_peak_rms(b'\x01') == 0.0


# ── the file-level gate ──────────────────────────────────────────────────────

def test_silent_wav_is_reported_as_silence():
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    _wav(p, [0] * 16000 * 6)
    assert wt._audio_is_silent(p) is True


def test_speech_wav_is_not_silence():
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    _wav(p, _tone(16000 * 2, 6000))
    assert wt._audio_is_silent(p) is False


def test_unreadable_format_fails_open():
    """A format the gate cannot decode must NOT be treated as silence.

    whisper_transcribe accepts WebM/MP3 too.  Guessing "silent" on an
    undecodable container would silently swallow real speech, so the gate
    abstains instead.
    """
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as f:
        f.write(b'\x1a\x45\xdf\xa3 not really webm')
        p = f.name
    assert wt._audio_is_silent(p) is False


def test_missing_file_fails_open():
    assert wt._audio_is_silent(r'C:\nope\does\not\exist.wav') is False


def test_stereo_wav_handled():
    """Mono is assumed by _pcm_rms; a stereo file must not crash or misread."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    with wave.open(p, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        mono = _tone(16000, 6000)
        inter = []
        for s in mono:
            inter.extend((s, s))
        w.writeframes(array.array('h', inter).tobytes())
    assert wt._audio_is_silent(p) is False


# ── chokepoint wiring ────────────────────────────────────────────────────────

def test_transcribe_impl_drops_text_from_silent_audio(monkeypatch):
    """_transcribe_impl is documented as "the ONE place every engine's result is
    post-filtered", so the gate belongs there and covers sherpa too (which has
    never applied the per-segment gate)."""
    import json
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    _wav(p, [0] * 16000 * 6)

    monkeypatch.setattr(wt, '_run_engine_chain',
                        lambda path, lang: json.dumps(
                            {'text': "I'm really happy with the reaction.",
                             'language': 'en'}))
    out = json.loads(wt._transcribe_impl(p))
    assert out['text'] == '', (
        f"silent audio produced {out['text']!r} — the hallucination still "
        f"reaches chat")
    assert out['language'] == 'unknown'


def test_transcribe_impl_keeps_text_from_real_speech(monkeypatch):
    """Zero-regression: real speech must pass through untouched."""
    import json
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    _wav(p, _tone(16000 * 2, 6000))

    monkeypatch.setattr(wt, '_run_engine_chain',
                        lambda path, lang: json.dumps(
                            {'text': 'turn on the kitchen light',
                             'language': 'en'}))
    out = json.loads(wt._transcribe_impl(p))
    assert out['text'] == 'turn on the kitchen light'
    assert out['language'] == 'en'
