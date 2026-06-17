"""STT anti-hallucination gate (#159).

A noise/silence window must NOT surface as a (hallucinated) phrase, and its
auto-detected language must NOT leak to the caller as if a human had spoken it
(the live "さて、さて、もみ" → Japanese reply incident).  vad_filter=True strips
silent audio regions but a short noise burst still decodes to a hallucinated
segment; the post-decode no_speech_prob/avg_logprob gate is what catches it.

Behavioural: real _filter_speech_text / _faster_whisper_transcribe /
_legacy_transcribe; mock ONLY the model boundary; assert observable text +
language.  No grep asserts.

    python -m pytest tests/unit/test_stt_no_speech_gate.py --noconftest -q
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

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
