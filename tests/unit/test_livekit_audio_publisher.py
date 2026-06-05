"""Behavioural tests for livekit_audio_publisher (#64 reply-audio publish half).

Loads the module by file path (bypassing integrations.social/__init__, which is
heavy) and monkeypatches its `livekit_rtc` with fakes, so the WHOLE publish path
— connect → publish_track → push_pcm → resample → 10ms framing → capture_frame —
is exercised WITHOUT the real native SDK installed. Mirrors the seam style of
test_livekit_transcript_subscriber.py. No grep/source-shape assertions.
"""
import importlib.util
import os
import time
import types

_HERE = os.path.dirname(__file__)
_MOD_PATH = os.path.normpath(os.path.join(
    _HERE, '..', '..', 'integrations', 'social', 'livekit_audio_publisher.py'))


def _load_module():
    spec = importlib.util.spec_from_file_location('lap_under_test', _MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


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


# ── Pure helpers ────────────────────────────────────────────────────

def test_iter_frames_chunks_and_zero_pads():
    P = _load_module()
    # 10ms @ 48k mono = 480 samples = 960 bytes/frame.  2000 bytes → 3 frames,
    # last zero-padded to a full frame.
    frames = list(P._iter_frames(b'\x01\x02' * 1000, 48000, 1, 10))
    assert [len(f) for f in frames] == [960, 960, 960]
    assert frames[-1].endswith(b'\x00')  # padding present


def test_resample_passthrough_and_upsample():
    P = _load_module()
    assert P._resample_pcm(b'ABCD', 48000, 1, 48000, 1) == b'ABCD'
    up = P._resample_pcm(b'\x00\x01' * 240, 24000, 1, 48000, 1)
    assert len(up) > 240 * 2 * 1.5  # ~doubled going 24k→48k


# ── Full publish path (fakes) ───────────────────────────────────────

def test_start_publishes_track_and_streams_frames():
    P = _load_module()
    P.HAS_LIVEKIT_RTC = True
    P.livekit_rtc = _fake_rtc()

    src = _FakeSource(48000, 1)
    pub = P.LiveKitAudioPublisher(
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


def test_no_sdk_start_is_noop():
    P = _load_module()
    P.HAS_LIVEKIT_RTC = False
    pub = P.LiveKitAudioPublisher('c', 'ws://x', 't')  # no room_factory
    assert pub.start() is False
    assert pub.is_alive() is False
    # push before start is a safe no-op, never raises
    assert pub.push_pcm(b'\x00\x01' * 100, 24000, 1) is False


def test_missing_fields_no_start():
    P = _load_module()
    P.HAS_LIVEKIT_RTC = True
    P.livekit_rtc = _fake_rtc()
    assert P.LiveKitAudioPublisher('', 'ws://x', 't',
                                   room_factory=_FakeRoom).start() is False
    assert P.LiveKitAudioPublisher('c', '', 't',
                                   room_factory=_FakeRoom).start() is False
    assert P.LiveKitAudioPublisher('c', 'ws://x', '',
                                   room_factory=_FakeRoom).start() is False


if __name__ == '__main__':
    # Standalone runner (this box OOMs pytest).
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = 0
    for fn in fns:
        fn()
        print('PASS', fn.__name__)
        passed += 1
    print(f'\n{passed}/{len(fns)} passed')
