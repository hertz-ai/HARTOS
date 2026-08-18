"""STT anti-hallucination gates (#159, #604, #661).

Three layered gates answer one question — "did a human actually speak?" — and
they live together because touching one without seeing the others is how the
later ones got missed for months.

GATE 1, probabilistic (#159).  A noise/silence window must NOT surface as a
(hallucinated) phrase, and its auto-detected language must NOT leak to the
caller as if a human had spoken it (the live "さて、さて、もみ" → Japanese reply
incident).  vad_filter=True strips silent audio regions but a short noise burst
still decodes to a hallucinated segment; the post-decode
no_speech_prob/avg_logprob gate is what catches it.

GATE 2, textual (#604).  Whisper is TRAINED to emit bracketed annotations for
non-speech audio — "[Music]", "(applause)" — and it emits them confidently, so
no_speech_prob stays low, avg_logprob stays high, and gate 1 passes them
straight through.  Live 2026-08-04 on the shipped build: the composer held
"(sad music)" and the assistant answered a turn whose entire content was
"(audience laughing)".  Auto-send fires 1s after a final transcript, so an
annotation becomes a user message with no human involved.

GATE 3, energy (#661).  Gates 1 and 2 both ask "does this look like a
caption?".  Whisper also fabricates fluent, grammatical PROSE from silence and
emits it confidently, so it has no bracket to match and no bad score to fail
on.  Live 2026-08-17, conv 730fa326 (media_mode=audio, casual=True) answered
four phantom utterances — "I'm really happy with the reaction.", "I don't think
we're here." and two more.  Nobody said them.  The real STT inputs from that
window measured 0-173 peak RMS against a threshold of 400.  The question that
separates these cases is "was anyone speaking?", which is energy, not text.

Diarization would answer it too, and integrations/audio/diarization_service.py
exists — but whisperx and pyannote.audio are not installed, so
_is_whisperx_available() is False and the service never starts.  A gate built
on it could not fire.  Energy needs no dependency.

Gates 2 and 3 sit at `_transcribe_impl`, the one point every engine returns
through, because gate 1 is applied INSIDE two of the three engines and
`_sherpa_transcribe` has never had it — exactly the bypass this placement makes
impossible.

Behavioural: real _filter_speech_text / _drop_non_speech_text /
_audio_is_silent / _faster_whisper_transcribe / _legacy_transcribe; mock ONLY
the model boundary; assert observable text + language.

    python -m pytest tests/unit/test_stt_no_speech_gate.py --noconftest -q
"""
import array
import ast
import inspect
import json
import math
import tempfile
import textwrap
import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import integrations.service_tools.whisper_tool as wt


def _seg(text, no_speech_prob, avg_logprob):
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob,
                           avg_logprob=avg_logprob)


# ── the pure gate ─────────────────────────────────────────────────────────
def test_filter_drops_high_no_speech_prob():
    # A confident-looking hallucination (good logprob) but high no_speech_prob
    # is still dropped — the "さて、さて、もみ on near-silence" case.
    assert wt._filter_speech_text([("さて、さて、もみ", 0.92, -0.3)]) == ""


def test_filter_drops_low_logprob():
    assert wt._filter_speech_text([("garbled", 0.1, -2.5)]) == ""


def test_filter_keeps_real_speech():
    assert wt._filter_speech_text([("hello there", 0.05, -0.4)]) == "hello there"


def test_filter_missing_signals_treated_as_speech():
    # A backend that doesn't report the signals must not be over-dropped.
    assert wt._filter_speech_text([("hi", None, None)]) == "hi"


def test_filter_mixed_keeps_only_speech():
    text = wt._filter_speech_text([
        ("real words", 0.10, -0.5),
        ("ご視聴ありがとう", 0.95, -0.2),   # hallucination on noise
    ])
    assert text == "real words"


# ── faster-whisper path: noise → empty text + 'unknown' language ───────────
def test_faster_whisper_noise_returns_empty_unknown():
    fake_model = SimpleNamespace(transcribe=lambda path, **kw: (
        [_seg("さて、さて、もみ", 0.93, -0.25)],
        SimpleNamespace(language="ja", language_probability=0.4),
    ))
    with patch.object(wt, '_whisper_load_breaker', None), \
         patch.object(wt, '_get_faster_whisper_model', return_value=fake_model), \
         patch.object(wt, '_record_whisper_success'):
        out = json.loads(wt._faster_whisper_transcribe("/tmp/x.wav"))
    assert out["text"] == ""
    assert out["language"] == "unknown"   # NOT 'ja' hallucinated from noise


def test_faster_whisper_speech_returns_text_and_language():
    fake_model = SimpleNamespace(transcribe=lambda path, **kw: (
        [_seg("hello", 0.05, -0.4)],
        SimpleNamespace(language="en", language_probability=0.98),
    ))
    with patch.object(wt, '_whisper_load_breaker', None), \
         patch.object(wt, '_get_faster_whisper_model', return_value=fake_model), \
         patch.object(wt, '_record_whisper_success'):
        out = json.loads(wt._faster_whisper_transcribe("/tmp/x.wav"))
    assert out["text"] == "hello"
    assert out["language"] == "en"


# ── legacy openai-whisper path uses the SAME gate (no parallel filter) ─────
def test_legacy_noise_returns_empty_unknown():
    fake_model = SimpleNamespace(transcribe=lambda path, **kw: {
        "text": "Thank you for watching",
        "language": "en",
        "segments": [{"text": "Thank you for watching",
                      "no_speech_prob": 0.90, "avg_logprob": -0.3}],
    })
    with patch.object(wt, '_select_legacy_model', return_value='base'), \
         patch.object(wt, '_get_whisper_model', return_value=fake_model):
        out = json.loads(wt._legacy_transcribe("/tmp/x.wav"))
    assert out["text"] == ""
    assert out["language"] == "unknown"


def test_legacy_speech_returns_text_and_language():
    fake_model = SimpleNamespace(transcribe=lambda path, **kw: {
        "text": "good morning",
        "language": "en",
        "segments": [{"text": "good morning",
                      "no_speech_prob": 0.08, "avg_logprob": -0.5}],
    })
    with patch.object(wt, '_select_legacy_model', return_value='base'), \
         patch.object(wt, '_get_whisper_model', return_value=fake_model):
        out = json.loads(wt._legacy_transcribe("/tmp/x.wav"))
    assert out["text"] == "good morning"
    assert out["language"] == "en"


# ══ GATE 2: the textual gate — annotations gate 1 is blind to (#604) ═══════
#
# Every string below was produced by the live shipped build, not invented.
@pytest.mark.parametrize("annotation", [
    "(sad music)",                              # composer, 04:39
    "(audience laughing)",                      # auto-sent; the assistant replied
    "(audience chattering)",
    "(air whooshing)",
    "(swoosh) (swoosh) (swoosh) (swoosh)",      # repeated → still all annotation
    "[Music]",
    "[silence]",
    "[BLANK_AUDIO]",
    "[ Applause ]",                             # padded
    "♪ ♪",                                      # music glyphs, no words
    "(音楽)",                                    # Whisper annotates in-language too
    "  [Music]  \n ",                           # surrounding whitespace
    "(swoosh).",                                # trailing punctuation only
    # ── DECISION FLIPPED 2026-08-07: unclosed annotation fragments ──────
    # "(swoosh" used to sit in the KEEP list below ("unmatched bracket →
    # keep") — the conservative default.  Three live incidents priced that
    # default: the streaming path truncates an annotation MID-TOKEN, the
    # missing ")" defeats the balanced-span regex, and the fragment reaches
    # chat as a user turn.  Shipped-build evidence: composer "(swoosh"
    # (2026-08-04), composer "(sizzling) (sizzling) (s" (#615, 2026-08-04),
    # and auto-SENT "(crick" answered by the assistant with a
    # "Crickets/Cricket/cramp?" reply (2026-08-07, installed app).  An
    # utterance that is ENTIRELY an unclosed annotation fragment is now
    # annotation; real words before the fragment still win (kept, below).
    "(crick",                                   # the 2026-08-07 auto-sent turn
    "(swoosh",                                  # was in the keep-list; flipped
    "(sizzling) (sizzling) (s",                 # #615 — balanced pair + tail
    "[Mus",                                     # square-bracket variant
])
def test_pure_annotation_is_dropped(annotation):
    assert wt._drop_non_speech_text(annotation) == ""


# The conservative half of the contract, and the more important one: a false
# positive here DELETES a user's sentence.  Anything with a word outside the
# brackets comes back byte-identical.
@pytest.mark.parametrize("speech", [
    "hello there",
    "I paid fifty (fifty!) dollars",            # real parenthetical aside
    "the [main] point is this",
    "Hello [Music] world",                      # mixed → keep, don't half-strip
    "こんにちは",                                 # CJK is \w — must not be dropped
    "नमस्ते",                                    # Devanagari likewise
    "I paid fifty (fifty",                      # real words BEFORE an unclosed
                                                # fragment → byte-identical, the
                                                # all-or-nothing rule unchanged
])
def test_real_speech_is_returned_untouched(speech):
    assert wt._drop_non_speech_text(speech) == speech


def test_empty_and_none_are_safe():
    assert wt._drop_non_speech_text("") == ""
    assert wt._drop_non_speech_text(None) == ""


# ── the chokepoint: applies to whatever the engine chain returned ──────────
def test_chokepoint_drops_annotation_and_disowns_the_language():
    """Mirrors gate 1's own convention: nothing spoken → language 'unknown',
    never the language Whisper inferred from music."""
    with patch.object(wt, '_run_engine_chain',
                      return_value=json.dumps({"text": "(sad music)",
                                               "language": "en"})):
        out = json.loads(wt._transcribe_impl("/tmp/x.wav"))
    assert out["text"] == ""
    assert out["language"] == "unknown"


def test_chokepoint_passes_real_speech_through_unchanged():
    with patch.object(wt, '_run_engine_chain',
                      return_value=json.dumps({"text": "hello there",
                                               "language": "en"})):
        out = json.loads(wt._transcribe_impl("/tmp/x.wav"))
    assert out["text"] == "hello there"
    assert out["language"] == "en"


def test_chokepoint_passes_engine_errors_through_verbatim():
    err = json.dumps({"error": "No STT engine available (install faster-whisper)"})
    with patch.object(wt, '_run_engine_chain', return_value=err):
        assert wt._transcribe_impl("/tmp/x.wav") == err


def test_no_engine_can_bypass_the_gate():
    """THE structural point of #604.

    Gate 1 is applied inside _faster_whisper_transcribe and _legacy_transcribe
    but NOT _sherpa_transcribe — a per-engine gate that one engine silently
    skips.  Gate 2 must never repeat that: if engine dispatch moves back into
    _transcribe_impl, a later `return result` can sidestep the filter again.

    Matches CALL expressions via AST rather than substrings, because
    _transcribe_impl's docstring names _sherpa_transcribe to explain exactly
    this — a text scan flags that prose and fails on the fix it is guarding.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(wt._transcribe_impl)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for engine in ('_faster_whisper_transcribe', '_sherpa_transcribe',
                   '_legacy_transcribe'):
        assert engine not in called, (
            f'{engine} is called inside _transcribe_impl. Engine selection '
            f'belongs in _run_engine_chain so every result passes the '
            f'annotation gate on the way out.')
    assert '_run_engine_chain' in called, (
        '_transcribe_impl no longer delegates to _run_engine_chain — the gate '
        'is only meaningful if it wraps the whole engine ladder')


# ── gate 3: energy ────────────────────────────────────────────────────────
# The sparse-speech cases are the regression guards that matter here: a gate
# that drops real quiet speech is worse than the bug it fixes.

def _wav_path(samples, rate=16000, channels=1):
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        p = f.name
    with wave.open(p, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array('h', samples).tobytes())
    return p


def _tone(n, amp):
    return [int(amp * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]


def test_peak_rms_finds_speech_in_a_mostly_silent_file():
    """2s of speech inside 30s of silence is SPEECH.

    A single RMS over the whole file averages this below the threshold and
    would discard a real utterance.  _pcm_rms is also wrong here — it reads
    only the trailing ~100ms, so speech ending quietly reads as silence.
    """
    pcm = array.array('h', [0] * (16000 * 28) + _tone(16000 * 2, 6000)).tobytes()
    peak = wt._pcm_peak_rms(pcm)
    assert peak > wt.STREAM_VAD_RMS_THRESHOLD, (
        f'peak RMS {peak:.0f} — real speech in a long file scored as silence; '
        f'this would drop genuine utterances')


def test_peak_rms_speech_at_the_very_start_survives():
    pcm = array.array('h', _tone(16000 * 2, 6000) + [0] * (16000 * 20)).tobytes()
    assert wt._pcm_peak_rms(pcm) > wt.STREAM_VAD_RMS_THRESHOLD


def test_peak_rms_true_silence_is_silence():
    assert wt._pcm_peak_rms(array.array('h', [0] * 16000 * 6).tobytes()) \
        < wt.STREAM_VAD_RMS_THRESHOLD


def test_peak_rms_low_level_noise_is_silence():
    """The live shape: 6s at peak ~173, well under 400, produced a sentence."""
    pcm = array.array('h', [80 if i % 2 else -80 for i in range(16000 * 6)]).tobytes()
    assert wt._pcm_peak_rms(pcm) < wt.STREAM_VAD_RMS_THRESHOLD


def test_peak_rms_empty_and_odd_length_are_safe():
    assert wt._pcm_peak_rms(b'') == 0.0
    assert wt._pcm_peak_rms(b'\x01') == 0.0


def test_silent_wav_is_reported_as_silence():
    assert wt._audio_is_silent(_wav_path([0] * 16000 * 6)) is True


def test_speech_wav_is_not_silence():
    assert wt._audio_is_silent(_wav_path(_tone(16000 * 2, 6000))) is False


def test_stereo_wav_handled():
    mono = _tone(16000, 6000)
    inter = []
    for s in mono:
        inter.extend((s, s))
    assert wt._audio_is_silent(_wav_path(inter, channels=2)) is False


def test_unreadable_format_fails_open():
    """whisper_transcribe accepts WebM/MP3 too.  Calling an undecodable
    container "silent" would swallow real speech, so the gate abstains."""
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as f:
        f.write(b'\x1a\x45\xdf\xa3 not really webm')
        p = f.name
    assert wt._audio_is_silent(p) is False


def test_missing_file_fails_open():
    assert wt._audio_is_silent(r'C:\nope\does\not\exist.wav') is False


def test_transcribe_impl_drops_prose_from_silent_audio():
    """The live case end to end: fluent prose over silent audio is dropped,
    and the language is not reported as if a human had spoken it."""
    p = _wav_path([0] * 16000 * 6)
    with patch.object(wt, '_run_engine_chain',
                      return_value=json.dumps(
                          {'text': "I'm really happy with the reaction.",
                           'language': 'en'})):
        out = json.loads(wt._transcribe_impl(p))
    assert out['text'] == '', (
        f'silent audio produced {out["text"]!r} — the hallucination still '
        f'reaches chat')
    assert out['language'] == 'unknown'


def test_transcribe_impl_keeps_text_from_real_speech():
    """Zero-regression: real speech passes through untouched."""
    p = _wav_path(_tone(16000 * 2, 6000))
    with patch.object(wt, '_run_engine_chain',
                      return_value=json.dumps(
                          {'text': 'turn on the kitchen light',
                           'language': 'en'})):
        out = json.loads(wt._transcribe_impl(p))
    assert out['text'] == 'turn on the kitchen light'
    assert out['language'] == 'en'
