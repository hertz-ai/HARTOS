"""Speech to text decodes only speech, and every decode is bounded (D22).

Live on the installed build, 2026-11-27 12:51-12:56 IST, the always-on mic
during the demo: windows of room noise were decoded with a guessed language
('ko' 0.16, 'nn' 0.47, 'jw' 0.22, 'ml' 0.79) and a decode after a wrong,
low-confidence guess ran 21 and 27 s; one worker decodes every window in
turn, so each runaway held every window behind it (transcription stopped
for 20-70 s).  Owner, the same evening, on a fix that pinned a language:
"multilanguage lang agnostic".

So, in integrations/service_tools/whisper_tool._faster_whisper_transcribe:
  * the bundled Silero VAD runs FIRST; a window it hears no speech in is
    answered '' with no model load, no decode and no language;
  * a window with speech is decoded with max_new_tokens that grows with its
    speech seconds, from MEASURED Whisper tokens per second (Tamil 18.6 at
    most, English 6.1, Tamil-then-English 7.7; the bound is twice the
    fastest), never above Whisper's own window;
  * no language is pinned: each window detects its own (multilingual).

Real: _faster_whisper_transcribe, _decode_token_bound, _speech_in.  Faked:
the model boundary (_get_faster_whisper_model) and the VAD's answer at the
_speech_in seam, since faster-whisper is not installed in this venv; one test
drives _speech_in itself over stand-in faster_whisper modules.

    python -m pytest tests/unit/test_stt_decodes_only_speech_and_is_bounded.py -q
"""
import json
import math
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import integrations.service_tools.whisper_tool as wt


class _Model:
    """A faster-whisper model that records every decode it is asked for."""

    def __init__(self, text='hello', language='en', windows=((0, 3),)):
        self.calls = []
        self._text = text
        self._language = language
        # (seek, tokens emitted) per segment, as faster-whisper 1.2.1 hands
        # them back: every segment carries its window's seek and its tokens.
        self._windows = windows

    def transcribe(self, source, **kw):
        self.calls.append((source, kw))
        return ([SimpleNamespace(text=self._text, no_speech_prob=0.05,
                                 avg_logprob=-0.3, seek=seek,
                                 tokens=list(range(count)))
                 for seek, count in self._windows],
                SimpleNamespace(language=self._language,
                                language_probability=0.95))


def _run(model, speech=None, language=None):
    """_faster_whisper_transcribe on '/tmp/x.wav' with ``model`` loaded and
    the VAD answering ``speech`` ((audio, seconds), or an exception)."""
    if isinstance(speech, Exception):
        vad = patch.object(wt, '_speech_in', side_effect=speech)
    else:
        vad = patch.object(wt, '_speech_in', return_value=speech)
    loads = []

    def load(size):
        loads.append(size)
        return model

    with patch.object(wt, '_whisper_load_breaker', None), \
            patch.object(wt, '_get_faster_whisper_model', side_effect=load), \
            patch.object(wt, '_record_whisper_success'), vad:
        out = wt._faster_whisper_transcribe('/tmp/x.wav', language=language,
                                            model_size='small')
    return json.loads(out), loads


def test_a_window_with_no_speech_is_never_decoded():
    model = _Model(text='Nobody said this.', language='ml')
    out, loads = _run(model, speech=('AUDIO', 0.0))
    assert out == {'text': '', 'language': 'unknown'}
    assert model.calls == []
    assert loads == []          # the model is not even loaded


def test_a_window_with_speech_is_decoded_once_with_a_bound_from_its_speech():
    model = _Model()
    out, _ = _run(model, speech=('AUDIO', 3.0))
    assert out == {'text': 'hello', 'language': 'en'}
    assert len(model.calls) == 1
    source, kw = model.calls[0]
    assert source == 'AUDIO'            # the audio the VAD already read
    assert kw['max_new_tokens'] == math.ceil(3.0 * wt._DECODE_TOKENS_PER_SPEECH_SECOND)
    assert kw['vad_filter'] is True and kw['beam_size'] == 5


def test_no_language_is_pinned():
    model = _Model()
    _run(model, speech=('AUDIO', 2.0))
    _, kw = model.calls[0]
    assert 'language' not in kw
    assert kw['multilingual'] is True


def test_a_language_the_caller_names_is_kept():
    model = _Model(language='ta')
    _run(model, speech=('AUDIO', 2.0), language='ta')
    _, kw = model.calls[0]
    assert kw['language'] == 'ta'
    assert 'multilingual' not in kw


def test_the_bound_grows_with_speech_and_stays_inside_whispers_window():
    bounds = [wt._decode_token_bound(s) for s in (0.01, 0.5, 1, 2, 5, 10, 60)]
    assert bounds == sorted(bounds)
    assert bounds[0] == wt._DECODE_TOKENS_MIN
    assert bounds[-1] == wt._DECODE_TOKENS_MAX
    assert wt._DECODE_TOKENS_MAX < 448


# The fastest clips measured 2026-11-27 (tokens Whisper emitted, seconds of
# speech after the VAD), synthetic voices only: Tamil at a fast and a normal
# rate, English, and Tamil-then-English.
_MEASURED = [(36, 1.94), (40, 2.35), (63, 3.79), (37, 2.26), (49, 3.36),
             (43, 3.01), (20, 3.28), (31, 4.03)]


def test_a_fast_tamil_sentence_is_never_cut():
    for emitted, seconds in _MEASURED:
        assert wt._decode_token_bound(seconds) >= 1.5 * emitted, (emitted, seconds)


def test_a_vad_that_cannot_run_leaves_the_decode_as_it_was():
    model = _Model()
    out, _ = _run(model, speech=RuntimeError('no onnxruntime'))
    assert out == {'text': 'hello', 'language': 'en'}
    source, kw = model.calls[0]
    assert source == '/tmp/x.wav'
    assert 'max_new_tokens' not in kw


def test_a_vad_that_cannot_run_is_said_once_not_on_every_window(caplog):
    """Review of 2dc827841 (rv_d22, 2026-11-28): the VAD failure was logged
    at debug, so a node whose VAD cannot run decodes every window of room
    noise unbounded and nothing in the log says why."""
    with patch.object(wt, '_vad_failure_warned', False), \
            caplog.at_level('WARNING', logger=wt.logger.name):
        for _ in range(3):
            _run(_Model(), speech=RuntimeError('no onnxruntime'))
    said = [r.getMessage() for r in caplog.records if 'VAD' in r.getMessage()]
    assert len(said) == 1, said
    assert 'no onnxruntime' in said[0]


def _bound_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if 'reached its token bound' in r.getMessage()]


def test_a_decode_that_reaches_its_bound_says_so_with_the_numbers(caplog):
    """rv_d22: a sentence cut at the bound came back as a normal transcript
    ("The cat climbed up the tree and") with 0 log lines.  The line carries
    the speech seconds, the bound and the tokens emitted, so the rate the
    bound is built on can be re-measured from a live log."""
    bound = wt._decode_token_bound(2.0)
    with caplog.at_level('WARNING', logger=wt.logger.name):
        out, _ = _run(_Model(windows=((0, bound),)), speech=('AUDIO', 2.0))
    assert out['text'] == 'hello'           # what was heard is still returned
    said = _bound_warnings(caplog)
    assert len(said) == 1, said
    assert '2.00 s' in said[0] and f'bound {bound}' in said[0]
    assert f'emitted {bound}' in said[0]


def test_a_decode_under_its_bound_says_nothing(caplog):
    bound = wt._decode_token_bound(2.0)
    with caplog.at_level('WARNING', logger=wt.logger.name):
        _run(_Model(windows=((0, bound - 1),)), speech=('AUDIO', 2.0))
    assert _bound_warnings(caplog) == []


def test_the_bound_is_per_window_so_a_long_clip_is_not_called_cut(caplog):
    """max_new_tokens is per window of a long clip: two windows that each
    stay under the bound are not a cut, though together they pass it."""
    bound = wt._decode_token_bound(40.0)
    windows = ((0, bound - 5), (0, 2), (3000, bound - 5))
    with caplog.at_level('WARNING', logger=wt.logger.name):
        _run(_Model(windows=windows), speech=('AUDIO', 40.0))
    assert _bound_warnings(caplog) == []


def test_a_single_short_syllable_is_decoded():
    """rv_d22 R4: 0.3 s of speech is decoded, never dropped as too short
    (on the real small model it decoded with room to spare)."""
    model = _Model(text='pa')
    out, _ = _run(model, speech=('AUDIO', 0.3))
    assert out == {'text': 'pa', 'language': 'en'}
    _, kw = model.calls[0]
    assert kw['max_new_tokens'] == wt._decode_token_bound(0.3) >= wt._DECODE_TOKENS_MIN


def test_speech_in_asks_faster_whispers_own_vad():
    audio_mod = types.ModuleType('faster_whisper.audio')
    vad_mod = types.ModuleType('faster_whisper.vad')
    audio_mod.decode_audio = lambda path, sampling_rate: ('PCM', path, sampling_rate)
    vad_mod.get_speech_timestamps = lambda audio: [
        {'start': 0, 'end': 16000}, {'start': 32000, 'end': 40000}]
    pkg = types.ModuleType('faster_whisper')
    with patch.dict(sys.modules, {'faster_whisper': pkg,
                                  'faster_whisper.audio': audio_mod,
                                  'faster_whisper.vad': vad_mod}):
        audio, seconds = wt._speech_in('/tmp/y.wav')
    assert audio == ('PCM', '/tmp/y.wav', 16000)
    assert seconds == 1.5
