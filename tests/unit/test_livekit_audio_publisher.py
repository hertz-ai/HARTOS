"""Behavioural tests for livekit_audio_publisher (#64 reply-audio publish half).

Monkeypatches the module's `livekit_rtc` with fakes and injects fake room +
source via the constructor seams, so the WHOLE publish path — connect →
publish_track → push_pcm → resample → 10ms framing → capture_frame — is exercised
WITHOUT the real native SDK. Mirrors test_livekit_transcript_subscriber.py's seam
style; the shared lifecycle now lives in _livekit_room._LiveKitRoomThread. No
grep/source-shape assertions.
"""
from __future__ import annotations

import os
import sys
import time
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import integrations.social.livekit_audio_publisher as pubmod
from integrations.social.livekit_audio_publisher import (
    LiveKitAudioPublisher, _iter_frames,
)
from integrations.social._livekit_room import resample_pcm16, HAS_LIVEKIT_RTC


# ── Fakes (a livekit.rtc stand-in) ──────────────────────────────────

class _FakeFrame:
    def __init__(self, data, sample_rate, num_channels, samples_per_channel):
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class _FakeSource:
    def __init__(self, rate, channels):
        self.rate = rate
        self.channels = channels
        self.frames = []

    async def capture_frame(self, frame):
        self.frames.append(frame)


class _FakeLocalAudioTrack:
    @staticmethod
    def create_audio_track(name, source):
        return ('track', name, source)


class _FakeParticipant:
    def __init__(self):
        self.published = []

    async def publish_track(self, track, options):
        self.published.append((track, options))


class _FakeRoom:
    def __init__(self):
        self.local_participant = _FakeParticipant()
        self.connected = False
        self.disconnected = False

    async def connect(self, url, token):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True


def _fake_rtc():
    ns = types.SimpleNamespace()
    ns.Room = _FakeRoom
    ns.AudioSource = _FakeSource
    ns.LocalAudioTrack = _FakeLocalAudioTrack
    ns.TrackPublishOptions = lambda **k: ('opts', k)
    ns.TrackSource = types.SimpleNamespace(SOURCE_MICROPHONE='mic')
    ns.AudioFrame = _FakeFrame
    return ns


def _wait(pred, timeout=4.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ── Pure helpers (the shared resampler + the publish framing) ───────

def test_iter_frames_chunks_and_zero_pads():
    # 10ms @ 48k mono = 480 samples = 960 bytes/frame.  2000 bytes → 3 frames,
    # last zero-padded to a full frame.
    frames = list(_iter_frames(b'\x01\x02' * 1000, 48000, 1, 10))
    assert [len(f) for f in frames] == [960, 960, 960]
    assert frames[-1].endswith(b'\x00')


def test_resample_passthrough_and_upsample():
    assert resample_pcm16(b'ABCD', 48000, 1, 48000, 1) == b'ABCD'
    up = resample_pcm16(b'\x00\x01' * 240, 24000, 1, 48000, 1)
    assert len(up) > 240 * 2 * 1.5  # ~doubled going 24k→48k


# ── Full publish path (fakes via the constructor seams) ─────────────

def test_start_publishes_track_and_streams_frames(monkeypatch):
    monkeypatch.setattr(pubmod, 'livekit_rtc', _fake_rtc())
    src = _FakeSource(48000, 1)
    pub = LiveKitAudioPublisher(
        'call1', 'ws://sfu', 'tok',
        room_factory=_FakeRoom,
        source_factory=lambda r, c: src,
    )
    assert pub.start() is True
    assert _wait(lambda: pub._ready.is_set()), 'track was never published'
    assert pub._room.connected is True
    assert len(pub._room.local_participant.published) == 1

    # 100ms of 48k mono s16 = 4800 samples = 9600 bytes → exactly 10 frames.
    assert pub.push_pcm(b'\x01\x02' * 4800, 48000, 1) is True
    assert _wait(lambda: len(src.frames) >= 10), f'got {len(src.frames)} frames'
    assert len(src.frames) == 10
    assert all(f.samples_per_channel == 480 for f in src.frames)
    assert all(f.sample_rate == 48000 for f in src.frames)

    pub.stop()
    assert _wait(lambda: not pub.is_alive()), 'thread did not stop'
    assert pub._room.disconnected is True


def test_missing_fields_no_start(monkeypatch):
    monkeypatch.setattr(pubmod, 'livekit_rtc', _fake_rtc())
    # room_factory bypasses the lib-gate; missing required fields → no start.
    assert LiveKitAudioPublisher('', 'ws://x', 't',
                                 room_factory=_FakeRoom).start() is False
    assert LiveKitAudioPublisher('c', '', 't',
                                 room_factory=_FakeRoom).start() is False
    assert LiveKitAudioPublisher('c', 'ws://x', '',
                                 room_factory=_FakeRoom).start() is False


def test_no_sdk_start_is_noop(monkeypatch):
    # Force the SDK-absent path deterministically. The no-op behaviour must hold
    # whenever the realtime SDK isn't usable — NOT only when this env happens to
    # lack `livekit`. A full voice dev env / CI-with-voice-extras HAS livekit
    # installed (HAS_LIVEKIT_RTC True), which used to flip this assert and fail
    # the test. Patch the base-module flag start() actually reads.
    import integrations.social._livekit_room as roommod
    monkeypatch.setattr(roommod, 'HAS_LIVEKIT_RTC', False)
    pub = LiveKitAudioPublisher('c', 'ws://x', 't')   # no room_factory seam
    assert pub.start() is False
    assert pub.is_alive() is False
    assert pub.push_pcm(b'\x00\x01' * 100, 24000, 1) is False
