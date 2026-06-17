"""#131: server-side VAD pause-finalization for the streaming STT server.

The streaming handler previously finalized ONLY on an explicit client
{control:final} or the 30s max-buffer flush — the docstring's promise of
"when VAD detects a speech pause ... it transcribes" was never implemented.
A raw-PCM feeder (or any client that doesn't signal end-of-speech) would
otherwise never get a timely final.

These tests exercise the pure decision logic — _StreamVadGate (energy state
machine) and _pcm_rms (PCM16 RMS) — directly, with no model / numpy / sockets.
The handler wiring (compute rms on each chunk -> vad.update -> _emit_final) is
the thin I/O shell around this logic.

whisper_tool's module-level imports are light (json/os/tarfile/pathlib +
.registry); faster-whisper/torch are lazy-imported inside functions, so this
import stays cheap.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import array  # noqa: E402

from integrations.service_tools.whisper_tool import (  # noqa: E402
    _StreamVadGate,
    _pcm_rms,
    STREAM_VAD_RMS_THRESHOLD,
    STREAM_VAD_SILENCE_MS,
    STREAM_VAD_MIN_SPEECH_MS,
)

_LOUD = STREAM_VAD_RMS_THRESHOLD * 5      # clearly speech
_QUIET = STREAM_VAD_RMS_THRESHOLD / 8     # clearly silence


# ── _pcm_rms (pure energy) ───────────────────────────────────────────

def test_rms_of_digital_silence_is_zero():
    assert _pcm_rms(b'\x00\x00' * 200) == 0.0


def test_rms_of_loud_signal_is_high():
    loud = array.array('h', [10000] * 200).tobytes()
    assert _pcm_rms(loud) == 10000.0


def test_rms_empty_and_odd_length_are_safe():
    assert _pcm_rms(b'') == 0.0
    assert _pcm_rms(b'\x05') == 0.0           # 1 byte -> no whole sample


# ── _StreamVadGate (pure decision) ───────────────────────────────────

def test_fires_after_speech_then_silence_threshold():
    g = _StreamVadGate()
    # 400ms speech (>= min_speech 300ms)
    assert g.update(_LOUD, 200) is False
    assert g.update(_LOUD, 200) is False
    # silence accumulates; fires exactly when it crosses SILENCE_MS
    fired = [g.update(_QUIET, 200) for _ in range(4)]  # 200,400,600,800
    assert fired == [False, False, False, True]


def test_leading_silence_never_fires():
    """Pure silence with no prior speech must not finalize an empty buffer."""
    g = _StreamVadGate()
    assert not any(g.update(_QUIET, 200) for _ in range(20))  # 4s of silence


def test_brief_midspeech_pause_does_not_fire():
    g = _StreamVadGate()
    g.update(_LOUD, 400)                      # speech
    # 600ms pause (< 800ms) then speech resumes -> no final
    assert g.update(_QUIET, 200) is False
    assert g.update(_QUIET, 200) is False
    assert g.update(_QUIET, 200) is False
    assert g.update(_LOUD, 200) is False      # resumed speech resets silence
    # a later, full pause DOES fire
    assert any(g.update(_QUIET, 200) for _ in range(4)) is True


def test_rearms_after_firing():
    g = _StreamVadGate()
    g.update(_LOUD, 400)
    assert any(g.update(_QUIET, 200) for _ in range(4)) is True   # first utterance
    # next utterance must finalize independently
    g.update(_LOUD, 400)
    assert any(g.update(_QUIET, 200) for _ in range(4)) is True


def test_explicit_reset_clears_speech():
    g = _StreamVadGate()
    g.update(_LOUD, 400)
    g.reset()
    # after reset, silence alone (no fresh speech) must not fire
    assert not any(g.update(_QUIET, 200) for _ in range(6))


def test_thresholds_are_sane_defaults():
    assert STREAM_VAD_RMS_THRESHOLD > 0
    assert STREAM_VAD_SILENCE_MS >= STREAM_VAD_MIN_SPEECH_MS > 0
