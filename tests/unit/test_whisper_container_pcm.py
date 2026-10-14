"""whisper_tool._container_to_pcm: a WebM / Ogg / WAV chunk on the streaming
STT socket becomes 16 kHz mono PCM16 through core.media_bin, the one ffmpeg
runner (AMENDMENTS A9b, lane review P1).

Measured before this change, in one process: media_bin ran the pinned
imageio-ffmpeg 7.1, while _container_to_pcm shelled a bare 'ffmpeg', got
FileNotFoundError (there is no ffmpeg on this desktop's PATH) and handed
back the WebM blob itself.  The stream wrote those container bytes into its
PCM buffer as if they were samples; a 2 s utterance (19,991 bytes of WebM)
read as under 1 s of "audio" and was never transcribed.

The real binary encodes tiny lavfi tones in tmp_path.  PATH is emptied so a
bare 'ffmpeg' can never be found, whatever this box has installed; the
binary media_bin locates (IMAGEIO_FFMPEG_EXE or the imageio-ffmpeg package)
does not come from PATH.  The STT worker is replaced at ``_stt_call``.
"""
import asyncio
import json
import os
import tempfile
import wave

import pytest

from core import media_bin
from core.subprocess_safe import BoundedResult
from integrations.service_tools import whisper_tool
from tests.unit.reel_media_fixtures import requires_ffmpeg

#: One second of the stream's PCM: 16 kHz, mono, 2 bytes per sample.
BYTES_PER_S = (whisper_tool.STREAM_SAMPLE_RATE * whisper_tool.STREAM_CHANNELS
               * whisper_tool.STREAM_BYTES_PER_SAMPLE)
#: One 20 ms Opus frame of that PCM: the most an encoder's padding may add.
OPUS_FRAME_BYTES = BYTES_PER_S // 50
EBML = b'\x1a\x45\xdf\xa3'


@pytest.fixture
def no_ffmpeg_on_path(tmp_path, monkeypatch):
    empty = tmp_path / 'empty-path'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))


@pytest.fixture
def private_tempdir(tmp_path, monkeypatch):
    """The conversion's temp files land here, so the test can see them go."""
    where = tmp_path / 'tmp'
    where.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(where))
    return where


def _tone(tmp_path, name, *, seconds=1.0, codec='libopus', rate=48000, channels=1):
    """A 440 Hz tone (lavfi's default amplitude, 1/8) in the container ``name`` names."""
    out = str(tmp_path / name)
    result = media_bin.run_ffmpeg(
        ['-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'sine=frequency=440:sample_rate=%d:duration=%s' % (rate, seconds),
         '-ac', str(channels), '-c:a', codec, out], timeout_s=120)
    assert result.returncode == 0, result.stderr[-500:]
    with open(out, 'rb') as fh:
        return fh.read()


def _is_speech_level(pcm):
    return whisper_tool._pcm_peak_rms(pcm) > whisper_tool.STREAM_VAD_RMS_THRESHOLD


# ── the real binary ───────────────────────────────────────────────────────

@requires_ffmpeg
@pytest.mark.parametrize('name, codec, rate, channels, magic', [
    ('chunk.webm', 'libopus', 48000, 1, EBML),        # a browser MediaRecorder chunk
    ('chunk.ogg', 'libopus', 48000, 1, b'OggS'),
    ('chunk.wav', 'pcm_s16le', 44100, 2, b'RIFF'),    # resampled and mixed down
])
def test_a_container_chunk_becomes_16k_mono_pcm16_with_no_ffmpeg_on_path(
        tmp_path, no_ffmpeg_on_path, private_tempdir, name, codec, rate, channels, magic):
    blob = _tone(tmp_path, name, codec=codec, rate=rate, channels=channels)
    assert blob[:4] == magic
    pcm = whisper_tool._container_to_pcm(blob)
    assert isinstance(pcm, bytes) and pcm != blob
    assert len(pcm) % whisper_tool.STREAM_BYTES_PER_SAMPLE == 0
    assert abs(len(pcm) - BYTES_PER_S) <= OPUS_FRAME_BYTES
    assert _is_speech_level(pcm)
    assert os.listdir(private_tempdir) == []


@requires_ffmpeg
def test_a_corrupt_container_gives_nothing_rather_than_its_bytes(no_ffmpeg_on_path,
                                                                 private_tempdir):
    garbage = EBML + bytes(range(256)) * 8
    assert whisper_tool._container_to_pcm(garbage) is None
    assert os.listdir(private_tempdir) == []


class _Socket:
    """The streaming handler's websocket: scripted messages in, sends recorded."""

    class request:
        path = '/'

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self._each()

    async def _each(self):
        for message in self._messages:
            yield message

    async def send(self, payload):
        self.sent.append(payload)


@requires_ffmpeg
def test_the_stt_worker_hears_the_audio_of_a_webm_chunk(tmp_path, no_ffmpeg_on_path,
                                                         monkeypatch):
    # The user-visible path: a 2 s WebM/Opus blob, then {control: final}.
    # What reaches the STT worker must be that utterance as 16 kHz mono
    # PCM16, not the container's bytes dressed up as samples.
    blob = _tone(tmp_path, 'utterance.webm', seconds=2.0)
    heard = []

    def _worker(req):
        with wave.open(req['audio_path'], 'rb') as wf:
            heard.append((wf.getframerate(), wf.getnchannels(), wf.getsampwidth(),
                          wf.readframes(wf.getnframes())))
        return {'raw_json': json.dumps({'text': '', 'language': 'unknown'})}

    monkeypatch.setattr(whisper_tool, '_stt_call', _worker)
    asyncio.run(whisper_tool._stt_stream_handler(
        _Socket([blob, json.dumps({'control': 'final'})])))
    assert heard, 'nothing reached the STT worker'
    rate, channels, width, pcm = heard[-1]        # the final pass: the whole utterance
    assert (rate, channels, width) == (whisper_tool.STREAM_SAMPLE_RATE, 1, 2)
    assert abs(len(pcm) - 2 * BYTES_PER_S) <= OPUS_FRAME_BYTES
    assert pcm[:4] != EBML
    assert _is_speech_level(pcm)


# ── through media_bin, bounded ────────────────────────────────────────────

def test_the_conversion_runs_media_bins_ffmpeg_bounded(monkeypatch, private_tempdir):
    seen = []

    def _run(args, *, timeout_s):
        args = list(args)
        with open(args[args.index('-i') + 1], 'rb') as fh:
            seen.append((args, timeout_s, fh.read()))
        with open(args[-1], 'wb') as fh:
            fh.write(b'\x01\x00' * 8)
        return BoundedResult(0, '', '')

    monkeypatch.setattr(media_bin, 'run_ffmpeg', _run)
    assert whisper_tool._container_to_pcm(EBML + b'one chunk') == b'\x01\x00' * 8
    [(args, timeout_s, fed)] = seen
    assert fed == EBML + b'one chunk'
    assert args[args.index('-ar') + 1] == str(whisper_tool.STREAM_SAMPLE_RATE)
    assert args[args.index('-ac') + 1] == '1'
    assert args[args.index('-f') + 1] == 's16le'
    assert 0 < timeout_s <= 60
    assert os.listdir(private_tempdir) == []


@pytest.mark.parametrize('outcome', [
    BoundedResult(1, '', 'Invalid data found when processing input'),
    BoundedResult(-1, '', '', timed_out=True),
])
def test_a_failed_or_timed_out_conversion_gives_nothing(monkeypatch, no_ffmpeg_on_path,
                                                        private_tempdir, outcome):
    monkeypatch.setattr(media_bin, 'run_ffmpeg', lambda args, *, timeout_s: outcome)
    assert whisper_tool._container_to_pcm(EBML + b'one chunk') is None
    assert os.listdir(private_tempdir) == []


def test_with_no_ffmpeg_anywhere_a_chunk_is_dropped_and_said_once(monkeypatch, no_ffmpeg_on_path,
                                                                  private_tempdir, caplog):
    # Container bytes are never audio, so a chunk ffmpeg cannot decode is
    # dropped (raw PCM16 frames on the same socket still transcribe).  The
    # stream sends several chunks a second: one warning, not one per chunk.
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: None)
    monkeypatch.setattr(whisper_tool, '_container_ffmpeg_missing_warned', False)
    with caplog.at_level('WARNING', logger=whisper_tool.logger.name):
        assert whisper_tool._container_to_pcm(EBML + b'one chunk') is None
        assert whisper_tool._container_to_pcm(EBML + b'next chunk') is None
    warnings = [r for r in caplog.records
                if r.levelname == 'WARNING' and r.name == whisper_tool.logger.name]
    assert len(warnings) == 1
    assert 'ffmpeg' in warnings[0].getMessage()
    assert os.listdir(private_tempdir) == []
