"""The sherpa-onnx leg must never be handed a faster-whisper model name.

``select_whisper_model()`` returns values from TWO namespaces — a sherpa key
from ``_SHERPA_MODELS`` ("whisper-tiny", "moonshine-base", ...) *or* a
faster-whisper size string ("large-v3", ...) — and its own docstring says so.
``_transcribe_impl`` stage 2 fed that return value straight into
``_sherpa_transcribe`` without discriminating, so whenever the catalog picked a
faster-whisper entry the sherpa leg did:

    _SHERPA_MODELS["large-v3"]   ->  KeyError: 'large-v3'

``_sherpa_transcribe`` swallows that into a warning and returns None, so the
whole sherpa engine silently no-opped.

Observed live 2026-08-04 in a shipped build — 582 occurrences in 19.5 minutes,
one every ~2s, continuously:

    [whisper_tool] Selected stt-faster-whisper-large (stt) - fit=gpu, score=344
    [whisper_tool] sherpa-onnx transcription failed (large-v3): 'large-v3'

That was not merely noise. On that machine the ladder was:
  1. faster-whisper  -> ImportError (ctranslate2 partially initialised)
  2. sherpa-onnx     -> INSTALLED AND WORKING (1.12.29), defeated by this bug
  3. openai-whisper  -> not installed
so the one healthy engine was the one this bug disabled, and STT produced
nothing at all while the UI reported "GPU speech-to-text runtime ready".

The guard mirrors the membership test the module already uses at the other
resolution site (``sherpa_key in _SHERPA_MODELS`` inside select_whisper_model)
rather than introducing a second, different notion of "is this a sherpa key".
"""

import json
import sys
import types

import pytest

from integrations.service_tools import whisper_tool


@pytest.fixture
def fake_sherpa(monkeypatch):
    """Stage 2 opens with `import sherpa_onnx`; keep the test hermetic.

    Without this the test would silently pass on any machine that lacks
    sherpa-onnx, because the branch would exit through `except ImportError`
    before reaching the code under test.
    """
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))


@pytest.fixture
def no_faster_whisper(monkeypatch):
    """Force stage 1 to be skipped so stage 2 is actually exercised."""
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)


def _capture_recognizer_arg(monkeypatch):
    """Record every model_name that reaches the sherpa recognizer."""
    seen = []

    def _fake_get(model_name='whisper-tiny'):
        seen.append(model_name)
        raise RuntimeError('stop here — the argument is what is under test')

    monkeypatch.setattr(whisper_tool, '_get_sherpa_recognizer', _fake_get)
    return seen


def test_faster_whisper_size_is_not_passed_to_sherpa(
        monkeypatch, fake_sherpa, no_faster_whisper, tmp_path):
    """THE regression: catalog picks faster-whisper, sherpa must not get its name."""
    monkeypatch.setattr(whisper_tool, 'select_whisper_model', lambda: 'large-v3')
    seen = _capture_recognizer_arg(monkeypatch)
    monkeypatch.setattr(whisper_tool, '_legacy_transcribe', lambda *_a, **_k: None)

    audio = tmp_path / 'x.wav'
    audio.write_bytes(b'RIFF....WAVEfmt ')
    whisper_tool._transcribe_impl(str(audio))

    assert seen, 'the sherpa leg was never reached — test would be vacuous'
    for name in seen:
        assert name in whisper_tool._SHERPA_MODELS, (
            f'sherpa recognizer got {name!r}, which is not a key of '
            f'_SHERPA_MODELS — this raises KeyError({name!r}) and silently '
            f'disables the entire sherpa engine')


def test_a_real_sherpa_key_is_passed_through_unchanged(
        monkeypatch, fake_sherpa, no_faster_whisper, tmp_path):
    """The guard must not clobber a correctly-resolved sherpa model."""
    key = next(iter(whisper_tool._SHERPA_MODELS))
    monkeypatch.setattr(whisper_tool, 'select_whisper_model', lambda: key)
    seen = _capture_recognizer_arg(monkeypatch)
    monkeypatch.setattr(whisper_tool, '_legacy_transcribe', lambda *_a, **_k: None)

    audio = tmp_path / 'x.wav'
    audio.write_bytes(b'RIFF....WAVEfmt ')
    whisper_tool._transcribe_impl(str(audio))

    assert seen and seen[0] == key


def test_non_english_still_forces_a_multilingual_sherpa_model(
        monkeypatch, fake_sherpa, no_faster_whisper, tmp_path):
    """Pre-existing behaviour that must survive the guard.

    An English-only model (Moonshine) plus an explicit non-English language
    must be swapped for a multilingual one — and that substitute must itself
    be a valid sherpa key.
    """
    english_only = next(
        (k for k, v in whisper_tool._SHERPA_MODELS.items() if not v.get('multilingual')),
        None)
    if english_only is None:
        pytest.skip('no English-only model in _SHERPA_MODELS to exercise the swap')

    monkeypatch.setattr(whisper_tool, 'select_whisper_model', lambda: english_only)
    seen = _capture_recognizer_arg(monkeypatch)
    monkeypatch.setattr(whisper_tool, '_legacy_transcribe', lambda *_a, **_k: None)

    audio = tmp_path / 'x.wav'
    audio.write_bytes(b'RIFF....WAVEfmt ')
    whisper_tool._transcribe_impl(str(audio), language='hi')

    assert seen
    assert seen[-1] in whisper_tool._SHERPA_MODELS
    assert whisper_tool._SHERPA_MODELS[seen[-1]].get('multilingual'), (
        'non-English request landed on an English-only model')


def test_sherpa_transcribe_reports_the_bad_key_instead_of_raising(tmp_path):
    """_sherpa_transcribe stays defensive: an unknown key returns None, not a raise.

    Belt-and-braces. The guard above is the fix; this pins that the swallow
    behaviour itself is intentional so a future reader does not "helpfully"
    let the KeyError escape into the caller.
    """
    audio = tmp_path / 'x.wav'
    audio.write_bytes(b'RIFF....WAVEfmt ')
    assert whisper_tool._sherpa_transcribe(str(audio), 'definitely-not-a-model') is None


def test_select_whisper_model_returns_a_string(monkeypatch):
    """Anchor: the two-namespace contract is real, so the guard is load-bearing."""
    out = whisper_tool.select_whisper_model()
    assert isinstance(out, str) and out
    # Deliberately NOT asserting it is a sherpa key — it legitimately may not
    # be. That ambiguity is precisely why stage 2 needs its own check.
    json.dumps({'selected': out})
