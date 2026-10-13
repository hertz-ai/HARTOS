"""integrations.reel_studio.media.beats -- the beat grid a reel's cuts snap to.

WHERE THE GRID COMES FROM (plan U2, amendment A3)
  The posted reel is silent: she adds the song inside Instagram and starts
  it at the suggested time.  The cuts can follow the song's real beats only
  when she attaches the song's audio (B2).  ``reel_beat_grid`` is the one
  place that rule lives:
    * song audio attached and a clear beat found -> the song's own beats
      (source 'local_audio');
    * no audio, audio with no clear beat (silence, speech, noise:
      confidence < MIN_CONFIDENCE), or audio ffmpeg cannot read
      -> an exact SYNTHETIC_BPM (120) grid, source 'synthetic', confidence 0.
  The grid's ``source`` rides into the EDL and the reply's
  ``media.song.beats``, so the reply can say which one the cuts follow.
  Grid time 0 is the song at ``start_s``, where she starts it in Instagram.

HOW BEATS ARE FOUND (numpy only, no librosa)
  1. Decode with core.media_bin.ffmpeg_stream (bounded): mono float32 PCM at
     22,050 Hz from ``start_s`` for ``dur_s``.
  2. Onset envelope: STFT (11.6 ms hop, 46 ms Hann window) -> 40 mel-spaced
     bands from 30 Hz to 8 kHz -> dB over an 80 dB range -> spectral flux
     (the mean rise in dB per frame).
  3. Tempo: autocorrelation of the lightly smoothed envelope over 60-180
     BPM.  Each candidate period L scores ac(L) + 0.5 ac(2L) + 0.25 ac(4L)
     (Ellis 2007's duple weighting, extended to the bar), times a log-normal
     prior centred on 110 BPM, one octave wide.  If the onsets repeat
     nearly as strongly at half the winning period, the faster pulse wins.
     The lag is refined to a fraction of a frame.
  4. Beats: Ellis's dynamic-programming tracker (2007).  Each beat
     maximises onset strength minus a log-squared penalty for straying from
     the period, and weak beats are trimmed off both ends.  If the onsets
     midway between tracked beats are at least half as strong as the beats
     (a snare between kicks), the pulse is twice as fast and the beats are
     tracked again.
  5. Each beat moves to the sub-frame peak of its onset, plus a measured
     9 ms correction for the analysis window seeing an onset early.
  Confidence is the envelope's normalised autocorrelation at the beat
  period, or at twice it for an accent pattern: 0 for silence, near 0 for
  noise, near 1 for a steady drum track.

MEASURED, ON SYNTHETIC SIGNALS ONLY (numpy and ffmpeg aevalsrc click
tracks, synthesised kick/snare/hi-hat loops; no real song yet)
  * click tracks, 60-180 BPM at three phases each: tempo within 0.1 %,
    every beat within 7 ms;
  * drum loops, 78-165 BPM: every beat within 20 ms of a drum hit (13 ms at
    worst in one kit) and the tempo within 0.1 %, except that a loop at or
    under 92 BPM with an even hi-hat can read at the eighth-note rate, a
    grid that still holds every beat;
  * white and pink noise: confidence <= 0.09; a held chord with a melody
    off the beat: 0.06; 16, 22.05, 44.1 and 48 kHz input behave the same.
  KNOWN LIMIT: a 3-3-2 accent pattern (dappankuthu) above about 130 BPM can
  read as the dotted quarter (a 3:2 error); its beats still fall on
  eighth-note hits.  MIN_CONFIDENCE and the octave rules still need checking
  on her real songs (plan E2E-1).
"""
from __future__ import annotations

import logging
import math
import numbers
import os
from typing import Optional, Tuple

import numpy as np

from core import media_bin
from integrations.reel_studio.contracts import BEAT_SOURCES, BeatGrid

logger = logging.getLogger(__name__)

# ── public constants ──────────────────────────────────────────────────────

#: Rate song audio is decoded at; beat tracking needs no more bandwidth.
SAMPLE_RATE = 22050
#: The grid a reel follows when there is no usable song audio (A3/U2).
SYNTHETIC_BPM = 120.0
#: A detected grid below this confidence is not trusted and the reel uses
#: the synthetic grid.  Measured: noise <= 0.09, synthetic drum loops
#: >= 0.76.  Not yet measured on real songs.
MIN_CONFIDENCE = 0.25
#: Tempo search range for detected beats, in BPM.
BPM_RANGE = (60.0, 180.0)
#: Tempi a synthetic grid may use, in BPM.
SYNTHETIC_BPM_RANGE = (30.0, 300.0)
#: Song excerpt decoded by default (the longest reel is 30 s).
DEFAULT_DUR_S = 30.0
#: Longest excerpt decoded: 600 s of mono float32 at 22,050 Hz is 53 MB.
MAX_DUR_S = 600.0
#: Longest span a synthetic grid covers.
MAX_SYNTHETIC_S = 3600.0
#: Bound on one decode (a first run under antivirus can be slow).
DECODE_TIMEOUT_S = 60.0
#: Sources a detected grid may claim; 'synthetic' belongs to synthetic_grid.
DETECTED_SOURCES = tuple(s for s in BEAT_SOURCES if s != 'synthetic')

# ── tuning, measured as the module docstring says ─────────────────────────

_HOP_S = 256.0 / 22050.0          # analysis hop; the window is 4 hops
_BANDS = 40
_FMIN_HZ = 30.0
_FMAX_HZ = 8000.0
_TOP_DB = 80.0
_SILENCE_DBFS = -60.0             # quieter than this throughout: no beat
_MIN_ANALYSIS_S = 2.0             # two periods at the slowest tempo
_MIN_SAMPLE_RATE = 8000
_PRIOR_BPM = 110.0
_PRIOR_OCTAVES = 1.0
_DUPLE_WEIGHT = 0.5               # ac(2L)
_BAR_WEIGHT = 0.25                # ac(4L)
_TEMPO_SMOOTH_FRAMES = 1.5        # so a period between frames still lines up
_FASTER_AC_RATIO = 0.8            # half the period nearly as periodic -> faster
_MIDPOINT_RATIO = 0.5             # onsets between beats this strong -> faster
_TIGHTNESS = 100.0                # Ellis's penalty weight
_LATENCY_S = 0.009                # clicks read 12 ms early, drum loops 6.5 ms
_STFT_BLOCK = 512                 # frames per FFT block (bounded memory)
_CHUNK_SAMPLES = 1024             # decode chunk; a partial last chunk is dropped
_TIME_DECIMALS = 6


# ── the synthetic grid ────────────────────────────────────────────────────

def _is_real(value) -> bool:
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and math.isfinite(value))


def synthetic_grid(bpm: float, dur_s: float) -> BeatGrid:
    """An exact grid at ``bpm`` from 0 to ``dur_s`` inclusive.

    Beat k sits at k * 60 / bpm, computed rather than accumulated so it
    never drifts, rounded to the microsecond.  source 'synthetic',
    confidence 0 (nothing was heard), song_key None.  Raises ValueError for
    a bpm outside SYNTHETIC_BPM_RANGE or a dur_s outside 0..MAX_SYNTHETIC_S.
    """
    lo, hi = SYNTHETIC_BPM_RANGE
    if not (_is_real(bpm) and lo <= bpm <= hi):
        raise ValueError('synthetic_grid needs bpm in %g..%g, got %r' % (lo, hi, bpm))
    if not (_is_real(dur_s) and 0 <= dur_s <= MAX_SYNTHETIC_S):
        raise ValueError('synthetic_grid needs dur_s in 0..%g s, got %r' % (MAX_SYNTHETIC_S, dur_s))
    period = 60.0 / float(bpm)
    count = int(math.floor(dur_s / period + 1e-9)) + 1
    times = tuple(round(k * period, _TIME_DECIMALS) for k in range(count))
    return BeatGrid(bpm=float(bpm), times_s=times, source='synthetic', confidence=0.0)


# ── beats found in PCM ────────────────────────────────────────────────────

def _sample_rate(sr) -> int:
    if not (_is_real(sr) and float(sr).is_integer() and sr >= _MIN_SAMPLE_RATE):
        raise ValueError('sr must be a whole number of Hz >= %d, got %r' % (_MIN_SAMPLE_RATE, sr))
    return int(sr)


def _check_detected(source, song_key) -> None:
    if source not in DETECTED_SOURCES:
        raise ValueError('a detected grid has source in %s, got %r' % (DETECTED_SOURCES, source))
    if song_key is not None and not isinstance(song_key, str):
        raise ValueError('song_key must be a string or None, got %r' % (song_key,))


def _as_mono(pcm) -> np.ndarray:
    """float32 mono from (samples,) or (samples, channels), float or int."""
    data = np.asarray(pcm)
    kind = data.dtype
    if not (np.issubdtype(kind, np.floating) or np.issubdtype(kind, np.integer)):
        raise ValueError('pcm must hold numbers, got dtype %s' % kind)
    if data.ndim == 2:
        # A real signal has far more samples than channels, whichever way round.
        data = data.mean(axis=1 if data.shape[1] <= data.shape[0] else 0)
    elif data.ndim != 1:
        raise ValueError('pcm must be (samples,) or (samples, channels), got shape %s'
                         % (data.shape,))
    if np.issubdtype(kind, np.integer):
        data = data / (float(np.iinfo(kind).max) + 1.0)
    mono = np.asarray(data, dtype=np.float32)
    if not np.all(np.isfinite(mono)):
        raise ValueError('pcm holds NaN or infinite samples')
    return mono


def _no_beats(source: str, song_key: Optional[str]) -> BeatGrid:
    return BeatGrid(bpm=0.0, times_s=(), source=source, confidence=0.0, song_key=song_key)


def _mel(hz):
    return 2595.0 * np.log10(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _hz(mel):
    return 700.0 * (10.0 ** (np.asarray(mel, dtype=np.float64) / 2595.0) - 1.0)


def _mel_bank(sr: int, n_fft: int) -> np.ndarray:
    """Triangular bands evenly spaced in mel, as a (bands, bins) matrix."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    edges = _hz(np.linspace(_mel(_FMIN_HZ), _mel(min(_FMAX_HZ, sr / 2.0)), _BANDS + 2))
    low, mid, high = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    bank = np.maximum(0.0, np.minimum((freqs - low) / (mid - low), (high - freqs) / (high - mid)))
    return bank[bank.sum(axis=1) > 0]


def _onset_envelope(mono: np.ndarray, sr: int) -> Tuple[np.ndarray, float]:
    """Spectral flux per frame (frame t is centred on sample t * hop), and frames/s."""
    hop = max(1, int(round(_HOP_S * sr)))
    n_fft = 4 * hop
    padded = np.pad(mono, (n_fft // 2, n_fft // 2))
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
    window = np.hanning(n_fft + 1)[:-1].astype(np.float32)
    bank = _mel_bank(sr, n_fft).T
    bands = np.empty((len(frames), bank.shape[1]))
    for start in range(0, len(frames), _STFT_BLOCK):
        block = frames[start:start + _STFT_BLOCK] * window
        bands[start:start + len(block)] = (np.abs(np.fft.rfft(block, axis=1)) ** 2) @ bank
    fps = sr / float(hop)
    ref = float(bands.max())
    if not ref > 0:
        return np.zeros(len(frames)), fps
    db = 10.0 * np.log10(np.maximum(bands, ref * 10.0 ** (-_TOP_DB / 10.0)) / ref)
    flux = np.maximum(0.0, np.diff(db, axis=0)).mean(axis=1)
    return np.concatenate([[0.0], flux]), fps


def _smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    reach = int(math.ceil(3 * sigma))
    kernel = np.exp(-0.5 * (np.arange(-reach, reach + 1) / sigma) ** 2)
    return np.convolve(values, kernel / kernel.sum(), 'same')


def _autocorrelation(values: np.ndarray) -> Optional[np.ndarray]:
    """Unbiased autocorrelation of the mean-removed values, 1.0 at lag 0."""
    x = values - values.mean()
    n = len(x)
    size = 1 << (2 * n - 1).bit_length()
    spectrum = np.fft.rfft(x, size)
    ac = np.fft.irfft(spectrum * np.conj(spectrum), size)[:n]
    if not ac[0] > 0:
        return None
    return ac / ac[0] * n / (n - np.arange(n))


def _peak_near(values: np.ndarray, centres, radius: int) -> np.ndarray:
    """The largest value within ``radius`` of each centre (0 past the end)."""
    out = np.zeros(len(centres))
    for i, centre in enumerate(centres):
        lo, hi = max(0, centre - radius), min(len(values), centre + radius + 1)
        if lo < hi:
            out[i] = values[lo:hi].max()
    return out


def _parabolic(values: np.ndarray, i: int) -> float:
    """Sub-sample position of the peak at ``i`` (1 <= i <= len - 2)."""
    a, b, c = values[i - 1], values[i], values[i + 1]
    curve = a - 2.0 * b + c
    offset = 0.5 * (a - c) / curve if curve < 0 else 0.0
    return i + float(np.clip(offset, -0.5, 0.5))


def _lag_bounds(fps: float, n: int) -> Tuple[int, int]:
    slow, fast = BPM_RANGE
    return max(2, int(math.floor(60.0 * fps / fast))), min(n - 2, int(math.ceil(60.0 * fps / slow)))


def _tempo(env: np.ndarray, fps: float) -> Optional[Tuple[float, float]]:
    """(beat period in frames, confidence 0..1), or None when there is no pulse."""
    ac = _autocorrelation(_smooth(env, _TEMPO_SMOOTH_FRAMES))
    if ac is None:
        return None
    lo, hi = _lag_bounds(fps, len(ac))
    if hi <= lo:
        return None
    lags = np.arange(lo, hi + 1)
    rising = np.maximum(ac, 0.0)
    metrical = (rising[lags] + _DUPLE_WEIGHT * _peak_near(rising, 2 * lags, 1)
                + _BAR_WEIGHT * _peak_near(rising, 4 * lags, 2))
    prior = np.exp(-0.5 * (np.log2(60.0 * fps / lags / _PRIOR_BPM) / _PRIOR_OCTAVES) ** 2)
    lag = int(lags[int(np.argmax(metrical * prior))])
    half = int(round(lag / 2.0))
    if half >= lo:
        near = half - 1 + int(np.argmax(ac[half - 1:half + 2]))
        if near >= lo and ac[near] >= _FASTER_AC_RATIO * ac[lag]:
            lag = near
    confidence = max(float(ac[lag]), float(_peak_near(ac, [2 * lag], 1)[0]))
    return _parabolic(ac, lag), float(np.clip(confidence, 0.0, 1.0))


def _trim(local: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """Drop the weak beats off both ends (silence before and after the music)."""
    if len(beats) < 3:
        return beats
    strength = np.convolve(local[beats], [0.5, 1.0, 0.5], 'same')
    keep = np.flatnonzero(strength > 0.5 * math.sqrt(float(np.mean(strength ** 2))))
    return beats[keep[0]:keep[-1] + 1] if len(keep) else beats[:0]


def _track(env: np.ndarray, period: float) -> np.ndarray:
    """Beat frames by Ellis's dynamic programme for a beat ``period`` in frames."""
    spread = float(np.std(env, ddof=1))
    if not spread > 0:
        return np.empty(0, dtype=np.int64)
    reach = int(round(period))
    bump = np.exp(-0.5 * (np.arange(-reach, reach + 1) * 32.0 / period) ** 2)
    local = np.convolve(env / spread, bump, 'same')
    gaps = np.arange(int(round(2 * period)), int(round(period / 2)) - 1, -1)
    penalty = -_TIGHTNESS * np.log(gaps / period) ** 2
    back = np.full(len(local), -1, dtype=np.int64)
    score = np.zeros(len(local))
    floor = 0.01 * float(local.max())
    started = False
    for i in range(len(local)):
        prev = i - gaps
        # A predecessor before the first frame counts as a beat scoring 0.
        candidates = penalty + np.where(prev >= 0, score[np.maximum(prev, 0)], 0.0)
        best = int(np.argmax(candidates))
        score[i] = local[i] + candidates[best]
        if started or local[i] >= floor:
            started = True
            back[i] = prev[best] if prev[best] >= 0 else -1
    peaks = np.flatnonzero((score[1:-1] > score[:-2]) & (score[1:-1] >= score[2:])) + 1
    if not len(peaks):
        return np.empty(0, dtype=np.int64)
    strong = peaks[score[peaks] > 0.5 * float(np.median(score[peaks]))]
    if not len(strong):
        return np.empty(0, dtype=np.int64)
    chain = [int(strong.max())]
    while back[chain[-1]] >= 0:
        chain.append(int(back[chain[-1]]))
    return _trim(local, np.array(chain[::-1], dtype=np.int64))


def _midpoints_strong(env: np.ndarray, beats: np.ndarray) -> bool:
    """True when the onsets halfway between beats are nearly as strong as the beats."""
    on = float(np.median([env[max(0, b - 1):b + 2].max() for b in beats]))
    mids = np.round((beats[:-1] + beats[1:]) / 2.0).astype(np.int64)
    between = float(np.median([env[max(0, m - 2):m + 3].max() for m in mids]))
    return on > 0 and between >= _MIDPOINT_RATIO * on


def _refine(env: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """Beat frames moved to the sub-frame peak of their onset."""
    out = beats.astype(np.float64)
    for i, b in enumerate(beats):
        if 0 < b < len(env) - 1 and env[b] >= env[b - 1] and env[b] >= env[b + 1]:
            out[i] = _parabolic(env, int(b))
    return out


def beat_grid_from_pcm(pcm, sr: int, *, source: str = 'local_audio',
                       song_key: Optional[str] = None) -> BeatGrid:
    """Pure: the beats in ``pcm`` sampled at ``sr`` Hz.

    ``pcm`` is (samples,) or (samples, channels), float (-1..1) or integer.
    Times are seconds from the first sample, ascending, rounded to the
    microsecond; ``bpm`` is the tracked pulse.  ``source`` is
    'local_audio' (her attached song, B2) or 'song' (a grid from central's
    song preview, B1).

    No beat (under 2 s of audio, quieter than -60 dBFS throughout, or no
    pulse) -> bpm 0.0, no times, confidence 0.0.  That grid fails
    validate_edl's beats schema on purpose; reel_beat_grid falls back to
    the synthetic grid instead.  A grid with beats but a low confidence is
    returned as found; reel_beat_grid decides whether to trust it.
    Raises ValueError for a bad sample rate, source, song_key or pcm.
    """
    sr = _sample_rate(sr)
    _check_detected(source, song_key)
    mono = _as_mono(pcm)
    if len(mono) < _MIN_ANALYSIS_S * sr:
        return _no_beats(source, song_key)
    if float(np.max(np.abs(mono))) <= 10.0 ** (_SILENCE_DBFS / 20.0):
        return _no_beats(source, song_key)
    env, fps = _onset_envelope(mono, sr)
    found = _tempo(env, fps)
    if found is None:
        return _no_beats(source, song_key)
    period, confidence = found
    beats = _track(env, period)
    if (len(beats) >= 4 and period / 2.0 >= _lag_bounds(fps, len(env))[0]
            and _midpoints_strong(env, beats)):
        period /= 2.0
        beats = _track(env, period)
    times = _refine(env, beats) / fps + _LATENCY_S
    times = times[(times >= 0.0) & (times <= len(mono) / float(sr))]
    if len(times) < 2:
        return _no_beats(source, song_key)
    return BeatGrid(bpm=round(60.0 * fps / period, 3),
                    times_s=tuple(round(float(t), _TIME_DECIMALS) for t in times),
                    source=source, confidence=round(confidence, 3), song_key=song_key)


# ── beats found in a file ─────────────────────────────────────────────────

def beat_grid_from_audio(path, *, start_s: float = 0.0, dur_s: float = DEFAULT_DUR_S,
                         source: str = 'local_audio', song_key: Optional[str] = None,
                         timeout_s: float = DECODE_TIMEOUT_S) -> BeatGrid:
    """The beats of ``dur_s`` seconds of the audio file at ``path`` from ``start_s``.

    Grid time 0 is ``start_s`` in the song.  The audio is decoded once by
    core.media_bin.ffmpeg_stream, bounded by ``timeout_s``.  A window past
    the end of the song decodes nothing and has no beats.  Raises
    ValueError for a bad window (before ffmpeg runs), FFmpegFailed when
    ffmpeg cannot read the file (missing, no audio stream, corrupt) or times
    out, and MediaToolMissing when there is no ffmpeg.
    """
    if not (_is_real(start_s) and start_s >= 0):
        raise ValueError('start_s must be a number of seconds >= 0, got %r' % (start_s,))
    if not (_is_real(dur_s) and 0 < dur_s <= MAX_DUR_S):
        raise ValueError('dur_s must be in (0, %g] s, got %r' % (MAX_DUR_S, dur_s))
    _check_detected(source, song_key)
    args = ['-loglevel', 'error', '-ss', '%.3f' % start_s, '-t', '%.3f' % dur_s,
            '-i', os.path.abspath(os.fspath(path)),
            '-vn', '-sn', '-dn', '-ac', '1', '-ar', str(SAMPLE_RATE),
            '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1']
    chunks = media_bin.ffmpeg_stream(args, frame_bytes=4 * _CHUNK_SAMPLES, timeout_s=timeout_s)
    pcm = np.frombuffer(b''.join(chunks), dtype='<f4')
    return beat_grid_from_pcm(pcm, SAMPLE_RATE, source=source, song_key=song_key)


def reel_beat_grid(audio_path, *, dur_s: float, start_s: float = 0.0,
                   song_key: Optional[str] = None,
                   timeout_s: float = DECODE_TIMEOUT_S) -> BeatGrid:
    """The grid a reel's cuts follow (plan U2, amendment A3).

    ``audio_path`` is her attached song audio (store purpose 'song_audio')
    or None; ``start_s`` is where the song starts in the reel; ``dur_s`` is
    the longest reel the planner may cut, e.g. target_s plus
    QUALITY_BAR['duration']['tol_s'].

    Returns the song's own beats (source 'local_audio') when ffmpeg reads
    the file and the beat is clear (confidence >= MIN_CONFIDENCE);
    otherwise synthetic_grid(SYNTHETIC_BPM, dur_s), with a log line saying
    why.  If the song file is shorter than dur_s, its grid ends where the
    song does.  MediaToolMissing (no ffmpeg: nothing can be rendered
    either) and ValueError (a bad window) are raised, not hidden.
    """
    fallback = synthetic_grid(SYNTHETIC_BPM, dur_s)
    if audio_path is None:
        logger.info('no song audio attached; the reel follows a synthetic %g BPM grid',
                    SYNTHETIC_BPM)
        return fallback
    name = os.path.basename(os.fspath(audio_path))
    try:
        grid = beat_grid_from_audio(audio_path, start_s=start_s, dur_s=dur_s,
                                    song_key=song_key, timeout_s=timeout_s)
    except media_bin.FFmpegFailed as e:
        logger.warning('ffmpeg could not read the song audio %r (%s); the reel follows a '
                       'synthetic %g BPM grid', name, str(e)[:300], SYNTHETIC_BPM)
        return fallback
    if grid.confidence < MIN_CONFIDENCE:
        logger.info('no clear beat in the song audio %r (confidence %.3f < %.2f); the reel '
                    'follows a synthetic %g BPM grid', name, grid.confidence, MIN_CONFIDENCE,
                    SYNTHETIC_BPM)
        return fallback
    logger.info('the reel follows the beat of %r: %.1f BPM, %d beats, confidence %.2f',
                name, grid.bpm, len(grid.times_s), grid.confidence)
    return grid


__all__ = ['SAMPLE_RATE', 'SYNTHETIC_BPM', 'MIN_CONFIDENCE', 'BPM_RANGE', 'SYNTHETIC_BPM_RANGE',
           'DEFAULT_DUR_S', 'MAX_DUR_S', 'MAX_SYNTHETIC_S', 'DECODE_TIMEOUT_S',
           'DETECTED_SOURCES', 'synthetic_grid', 'beat_grid_from_pcm', 'beat_grid_from_audio',
           'reel_beat_grid']
