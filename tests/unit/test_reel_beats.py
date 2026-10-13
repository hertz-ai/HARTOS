"""integrations.reel_studio.media.beats: the beat grid a reel's cuts snap to (WS-2).

Behavioural: the real module is imported and called.  The pure path
(beat_grid_from_pcm, synthetic_grid) runs on numpy signals built here; the
file path (beat_grid_from_audio, reel_beat_grid) decodes real files that
ffmpeg's lavfi sources write into the test's own tmp dir
(tests/unit/reel_media_fixtures.py).  Only boundaries are replaced: the
network is sealed, and one test removes the ffmpeg binary.

"On the beat" is measured both ways: every true beat inside the window has a
detected beat within 20 ms, and every detected beat inside it is within 20 ms
of a true beat (no off-beat or spurious beats).
"""
import math
import os

import numpy as np
import pytest

from core import media_bin
from integrations.reel_studio import contracts
from integrations.reel_studio.media import beats
from tests.conftest import refuse_all_network
from tests.unit.reel_media_fixtures import (
    golden_edl, make_click_track, make_clip, requires_ffmpeg)

SR = 22050
#: How far a detected beat may sit from the true one (plan section 5).
BEAT_TOL_S = 0.020
#: How far the reported tempo may be from the true one (plan section 5).
BPM_TOL = 0.02


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_reel_beats: beat tracking is local')


# ── test signals ──────────────────────────────────────────────────────────

def _click_pcm(bpm, dur_s, offset_s=0.25, *, sr=SR, click_s=0.01, freq_hz=1000.0, amp=0.9):
    """The same click track make_click_track writes: a 10 ms 1 kHz burst per beat."""
    t = np.arange(int(round(dur_s * sr))) / sr
    on = (np.mod(t - offset_s, 60.0 / bpm) < click_s) & (t >= offset_s)
    return np.where(on, amp * np.sin(2 * np.pi * freq_hz * t), 0.0).astype(np.float32)


def _beat_times(bpm, dur_s, offset_s=0.25):
    period = 60.0 / bpm
    return offset_s + period * np.arange(int(math.floor((dur_s - offset_s) / period)) + 1)


def _drum_loop(bpm, dur_s, offset_s=0.25, *, seed=7, sr=SR):
    """A backbeat: kick on beats 1 and 3, snare on 2 and 4, a hi-hat on every
    eighth note, over a little noise.  Returns (pcm, beat times)."""
    rng = np.random.default_rng(seed)
    n = int(round(dur_s * sr))
    out = np.zeros(n)
    t = np.arange(int(0.2 * sr)) / sr
    attack = np.zeros(len(t))
    attack[:int(0.002 * sr)] = 0.6 * rng.standard_normal(int(0.002 * sr))
    kick = 0.8 * np.sin(2 * np.pi * (50 + 80 * np.exp(-t / 0.02)) * t) * np.exp(-t / 0.1) + attack
    snare = 0.5 * rng.standard_normal(len(t)) * np.exp(-t / 0.05)
    hat = 0.12 * np.diff(np.concatenate([[0.0], rng.standard_normal(len(t))])) * np.exp(-t / 0.01)

    def place(at_s, sound):
        start = int(round(at_s * sr))
        if start < n:
            end = min(n, start + len(sound))
            out[start:end] += sound[:end - start]

    times = _beat_times(bpm, dur_s, offset_s)
    period = 60.0 / bpm
    for k, at in enumerate(times):
        place(at, kick if k % 2 == 0 else snare)
        place(at, hat)
        place(at + period / 2, hat)
    out += 0.003 * rng.standard_normal(n)
    return out.astype(np.float32), times


def _assert_on_the_beat(grid, truth, lo_s, hi_s, tol_s=BEAT_TOL_S):
    found = np.asarray(grid.times_s)
    assert len(found) > 0, 'no beats were found'
    inner = truth[(truth >= lo_s) & (truth <= hi_s)]
    missed = [round(c, 3) for c in inner if np.min(np.abs(found - c)) > tol_s]
    assert not missed, 'true beats with no detected beat within %d ms: %s' % (tol_s * 1000, missed)
    stray = [round(x, 3) for x in found
             if lo_s <= x <= hi_s and np.min(np.abs(truth - x)) > tol_s]
    assert not stray, 'detected beats more than %d ms from any true beat: %s' % (tol_s * 1000, stray)


def _assert_tempo(grid, bpm):
    assert abs(grid.bpm - bpm) <= BPM_TOL * bpm, 'reported %.2f BPM for a %s BPM track' % (grid.bpm, bpm)


# ── the synthetic grid (no song audio: A3/U2) ─────────────────────────────

def test_synthetic_grid_is_exact_and_says_it_is_synthetic():
    grid = beats.synthetic_grid(120, 21.0)
    assert isinstance(grid, contracts.BeatGrid)
    assert grid.source == 'synthetic'
    assert grid.bpm == 120.0
    assert grid.confidence == 0.0
    assert grid.song_key is None
    assert list(grid.times_s) == [0.5 * k for k in range(43)]   # 0.0 .. 21.0 inclusive

    grid = beats.synthetic_grid(140, 20.0)
    period = 60.0 / 140
    times = np.asarray(grid.times_s)
    assert times[0] == 0.0
    assert np.allclose(np.diff(times), period, atol=1e-6)
    assert times[-1] <= 20.0 < times[-1] + period
    assert np.allclose(times, period * np.arange(len(times)), atol=1e-6)   # no drift


def test_a_synthetic_grid_drops_into_a_valid_edl():
    edl = golden_edl()
    edl['beats'] = beats.synthetic_grid(beats.SYNTHETIC_BPM, edl['target_s'] + 1.0).to_edl()
    assert contracts.validate_edl(edl) == []


@pytest.mark.parametrize('bpm, dur_s', [
    (0, 10.0), (-120, 10.0), (float('nan'), 10.0), (float('inf'), 10.0), (1000, 10.0),
    (120, -1.0), (120, float('nan')), (120, float('inf')), ('120', 10.0), (True, 10.0),
])
def test_a_synthetic_grid_refuses_nonsense(bpm, dur_s):
    with pytest.raises(ValueError):
        beats.synthetic_grid(bpm, dur_s)


# ── beats found in PCM ────────────────────────────────────────────────────

@pytest.mark.parametrize('bpm', [96, 120, 140])
def test_a_click_track_gives_its_tempo_and_its_beats(bpm):
    grid = beats.beat_grid_from_pcm(_click_pcm(bpm, 20.0), SR)
    assert grid.source == 'local_audio'
    _assert_tempo(grid, bpm)
    _assert_on_the_beat(grid, _beat_times(bpm, 20.0), 1.0, 19.0)
    assert grid.confidence >= beats.MIN_CONFIDENCE
    times = list(grid.times_s)
    assert times == sorted(times) and times[0] >= 0.0 and times[-1] <= 20.0


@pytest.mark.parametrize('bpm', [70, 170])
def test_the_octave_is_resolved_on_clicks(bpm):
    """170 is not reported as 85, nor 70 as 140 (the tempo prior is centred on 110)."""
    grid = beats.beat_grid_from_pcm(_click_pcm(bpm, 20.0), SR)
    _assert_tempo(grid, bpm)
    _assert_on_the_beat(grid, _beat_times(bpm, 20.0), 1.0, 19.0)


@pytest.mark.parametrize('bpm', [100, 150])
def test_a_backbeat_reads_at_the_beat_and_not_at_half_tempo(bpm):
    """Kick and snare alternate, so the sound repeats every two beats; the
    grid must still mark every beat, each on a kick or a snare."""
    pcm, truth = _drum_loop(bpm, 20.0)
    grid = beats.beat_grid_from_pcm(pcm, SR)
    _assert_tempo(grid, bpm)
    _assert_on_the_beat(grid, truth, 1.0, 19.0)
    assert grid.confidence >= beats.MIN_CONFIDENCE


def test_silence_has_no_beats_and_zero_confidence():
    for pcm in (np.zeros(20 * SR, np.float32),
                (1e-4 * np.random.default_rng(0).standard_normal(20 * SR)).astype(np.float32)):
        grid = beats.beat_grid_from_pcm(pcm, SR)
        assert grid.confidence == 0.0
        assert grid.times_s == ()
        assert grid.bpm == 0.0


def test_noise_is_not_trusted_as_a_beat():
    for seed in range(3):
        noise = (0.3 * np.random.default_rng(seed).standard_normal(20 * SR)).astype(np.float32)
        grid = beats.beat_grid_from_pcm(noise, SR)
        assert grid.confidence < beats.MIN_CONFIDENCE


def test_audio_too_short_for_a_tempo_has_no_beats():
    grid = beats.beat_grid_from_pcm(_click_pcm(120, 1.5, 0.1), SR)
    assert grid.confidence == 0.0 and grid.times_s == ()


def test_stereo_and_int16_pcm_give_the_same_grid():
    mono = _click_pcm(120, 12.0)
    want = beats.beat_grid_from_pcm(mono, SR)
    stereo = np.stack([mono, mono], axis=1)
    as_int16 = np.round(stereo * 32767).astype(np.int16)
    for pcm in (stereo, as_int16):
        got = beats.beat_grid_from_pcm(pcm, SR)
        assert got.bpm == pytest.approx(want.bpm, rel=1e-3)
        assert np.allclose(got.times_s, want.times_s, atol=0.002)


@pytest.mark.parametrize('sr', [16000, 44100, 48000])
def test_other_sample_rates_give_the_same_beats(sr):
    grid = beats.beat_grid_from_pcm(_click_pcm(120, 12.0, sr=sr), sr)
    _assert_tempo(grid, 120)
    _assert_on_the_beat(grid, _beat_times(120, 12.0), 1.0, 11.0)


def test_source_and_song_key_ride_along():
    grid = beats.beat_grid_from_pcm(_click_pcm(120, 6.0), SR, source='song', song_key='cluster:123')
    assert grid.source == 'song' and grid.song_key == 'cluster:123'
    assert grid.to_edl()['song_key'] == 'cluster:123'


@pytest.mark.parametrize('pcm, sr, kwargs', [
    (np.array([0.0, float('nan')] * SR, np.float32), SR, {}),
    (np.zeros((2, 2, SR), np.float32), SR, {}),
    (np.zeros(4 * SR, np.float32), 4000, {}),
    (np.zeros(4 * SR, np.float32), 22050.5, {}),
    (np.zeros(4 * SR, np.float32), SR, {'source': 'synthetic'}),
    (np.zeros(4 * SR, np.float32), SR, {'source': 'bogus'}),
    (np.zeros(4 * SR, bool), SR, {}),
])
def test_bad_pcm_is_refused(pcm, sr, kwargs):
    with pytest.raises(ValueError):
        beats.beat_grid_from_pcm(pcm, sr, **kwargs)


def test_a_detected_grid_drops_into_a_valid_edl():
    """The golden EDL cuts at 3, 7, 11 and 15 s; a real 120 BPM grid from
    beat 0 satisfies the contract's 80 ms beat check and its beats schema."""
    edl = golden_edl()
    grid = beats.beat_grid_from_pcm(_click_pcm(120, 21.5, offset_s=0.0), SR)
    edl['beats'] = grid.to_edl()
    assert contracts.validate_edl(edl) == []


def test_an_empty_grid_cannot_pass_as_a_valid_edl():
    """bpm 0 with no beats is refused by the contract, so a caller that skips
    the confidence check fails loudly instead of cutting at random."""
    edl = golden_edl()
    edl['beats'] = beats.beat_grid_from_pcm(np.zeros(5 * SR, np.float32), SR).to_edl()
    assert contracts.failed_checks(contracts.validate_edl(edl)) == ['schema']


# ── beats found in a file (ffmpeg decode) ─────────────────────────────────

def _write_audio(out_dir, name, source):
    """A lavfi audio source rendered to a WAV in the test's tmp dir."""
    out = os.path.join(os.fspath(out_dir), name)
    result = media_bin.run_ffmpeg(['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', source,
                                   '-ac', '1', out], timeout_s=120.0)
    assert result.returncode == 0 and not result.timed_out, result.stderr
    return out


@requires_ffmpeg
@pytest.mark.parametrize('bpm', [96, 120, 140])
def test_a_click_track_file_gives_its_tempo_and_its_beats(tmp_path, bpm):
    path = make_click_track(tmp_path, bpm, 20.0, offset_s=0.25)
    grid = beats.beat_grid_from_audio(path)
    assert grid.source == 'local_audio'
    _assert_tempo(grid, bpm)
    _assert_on_the_beat(grid, _beat_times(bpm, 20.0), 1.0, 19.0)
    assert grid.confidence >= beats.MIN_CONFIDENCE


@requires_ffmpeg
def test_start_s_and_dur_s_window_the_song(tmp_path):
    """Grid time 0 is the song at start_s (where she starts it in Instagram)."""
    path = make_click_track(tmp_path, 120, 20.0, offset_s=0.25)
    grid = beats.beat_grid_from_audio(path, start_s=3.1, dur_s=8.0)
    truth = _beat_times(120, 20.0) - 3.1
    _assert_on_the_beat(grid, truth[(truth >= 0) & (truth <= 8.0)], 1.0, 7.0)
    assert grid.times_s[-1] <= 8.0


@requires_ffmpeg
def test_the_song_is_decoded_by_the_one_bounded_runner(tmp_path, monkeypatch):
    path = make_click_track(tmp_path, 120, 6.0)
    real_stream = media_bin.ffmpeg_stream
    calls = []

    def spy(args, *, frame_bytes, timeout_s):
        calls.append((list(args), frame_bytes, timeout_s))
        return real_stream(args, frame_bytes=frame_bytes, timeout_s=timeout_s)

    monkeypatch.setattr(media_bin, 'ffmpeg_stream', spy)
    grid = beats.beat_grid_from_audio(path, start_s=0.5, dur_s=5.0, timeout_s=33.0)
    assert len(calls) == 1
    args, frame_bytes, timeout_s = calls[0]
    assert timeout_s == 33.0
    assert frame_bytes % 4 == 0
    assert args[args.index('-ss') + 1] == '0.500' and args[args.index('-t') + 1] == '5.000'
    assert args.index('-ss') < args.index('-i')            # input seeking
    assert args[args.index('-ac') + 1] == '1'
    assert args[args.index('-ar') + 1] == str(beats.SAMPLE_RATE)
    assert args[args.index('-f') + 1] == 'f32le' and args[-1] == 'pipe:1'
    assert args[args.index('-i') + 1] == os.path.abspath(path)
    _assert_tempo(grid, 120)


@requires_ffmpeg
def test_a_silent_file_has_no_beats(tmp_path):
    path = _write_audio(tmp_path, 'silence.wav', 'anullsrc=r=22050:cl=mono:d=6')
    grid = beats.beat_grid_from_audio(path)
    assert grid.confidence == 0.0 and grid.times_s == ()


@requires_ffmpeg
def test_a_window_past_the_end_of_the_song_has_no_beats(tmp_path):
    path = make_click_track(tmp_path, 120, 4.0)
    grid = beats.beat_grid_from_audio(path, start_s=60.0, dur_s=5.0)
    assert grid.confidence == 0.0 and grid.times_s == ()


@requires_ffmpeg
def test_audio_ffmpeg_cannot_read_raises_ffmpeg_failed(tmp_path):
    video_only = make_clip(tmp_path, 'no_audio.mp4', duration_s=1.0)
    with pytest.raises(media_bin.FFmpegFailed):
        beats.beat_grid_from_audio(video_only)
    with pytest.raises(media_bin.FFmpegFailed):
        beats.beat_grid_from_audio(os.path.join(os.fspath(tmp_path), 'missing.mp3'))


@pytest.mark.parametrize('kwargs', [
    {'start_s': -1.0}, {'start_s': float('nan')}, {'dur_s': 0.0}, {'dur_s': -3.0},
    {'dur_s': float('inf')}, {'dur_s': beats.MAX_DUR_S + 1}, {'source': 'synthetic'},
])
def test_a_bad_window_is_refused_before_ffmpeg_runs(tmp_path, monkeypatch, kwargs):
    def never(*a, **kw):
        raise AssertionError('ffmpeg must not run for a refused window')

    monkeypatch.setattr(media_bin, 'ffmpeg_stream', never)
    with pytest.raises(ValueError):
        beats.beat_grid_from_audio(os.path.join(os.fspath(tmp_path), 'song.mp3'), **kwargs)


# ── the reel's grid: her song's beats, else 120 BPM (A3/U2) ───────────────

@requires_ffmpeg
def test_the_reel_follows_her_song_when_it_has_a_beat(tmp_path):
    path = make_click_track(tmp_path, 96, 24.0, offset_s=0.25)
    grid = beats.reel_beat_grid(path, dur_s=21.0, song_key='cluster:7')
    assert grid.source == 'local_audio' and grid.song_key == 'cluster:7'
    _assert_tempo(grid, 96)
    assert grid.times_s[-1] <= 21.0


def test_no_song_audio_gives_the_synthetic_120_bpm_grid():
    grid = beats.reel_beat_grid(None, dur_s=21.0)
    assert grid == beats.synthetic_grid(120, 21.0)


@requires_ffmpeg
@pytest.mark.parametrize('kind', ['silence', 'noise', 'video_only', 'missing'])
def test_a_song_without_a_usable_beat_falls_back_to_the_synthetic_grid(tmp_path, kind, caplog):
    if kind == 'silence':
        path = _write_audio(tmp_path, 'silence.wav', 'anullsrc=r=22050:cl=mono:d=12')
    elif kind == 'noise':
        path = _write_audio(tmp_path, 'noise.wav', 'anoisesrc=r=22050:a=0.3:d=12:seed=3')
    elif kind == 'video_only':
        path = make_clip(tmp_path, 'no_audio.mp4', duration_s=1.0)
    else:
        path = os.path.join(os.fspath(tmp_path), 'missing.mp3')
    with caplog.at_level('INFO', logger=beats.__name__):
        grid = beats.reel_beat_grid(path, dur_s=12.0, song_key='cluster:9')
    assert grid == beats.synthetic_grid(beats.SYNTHETIC_BPM, 12.0)
    assert any('synthetic' in r.getMessage() for r in caplog.records), \
        'the fallback must say why the reel is not on her song'


def test_no_ffmpeg_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    """Without ffmpeg nothing can be rendered either; the pipeline must say so."""
    song = tmp_path / 'song.mp3'
    song.write_bytes(b'\0' * 16)
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: None)
    with pytest.raises(media_bin.MediaToolMissing):
        beats.reel_beat_grid(str(song), dur_s=12.0)
