"""whisper_tool.read_wav_f32 -- the one WAV reader -- reads what the engines
write.  STT's own 16-bit PCM read as it always did; a TTS engine's float
output (scipy.io.wavfile and soundfile write an IEEE-float WAV for a float
array, plain or WAVE_FORMAT_EXTENSIBLE), which the stdlib ``wave`` module
refuses with 'unknown format: 3', is read too: agent_voice_bridge speaks a
call's reply from it (review of ca1de342a, finding 2)."""
import struct
import wave

import numpy as np
import pytest

from integrations.service_tools import whisper_tool

SAMPLES = [0.0, 0.25, -0.5, 0.75, -1.0]


def _pcm16(path, frames, channels=1, rate=16000):
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)


def test_16_bit_pcm_reads_as_it_always_did(tmp_path):
    _pcm16(tmp_path / 'a.wav', struct.pack('<5h', 0, 8192, -16384, 24576, -32768))
    rate, data = whisper_tool.read_wav_f32(str(tmp_path / 'a.wav'))
    assert rate == 16000 and data.dtype == np.float32
    assert list(data) == SAMPLES


@pytest.mark.parametrize('dtype', ['float32', 'float64'])
def test_scipys_float_wav_is_read(tmp_path, dtype):
    import scipy.io.wavfile
    scipy.io.wavfile.write(str(tmp_path / 'a.wav'), 24000,
                           np.array(SAMPLES, dtype=dtype))
    with pytest.raises(wave.Error):  # the file is what wave refuses
        wave.open(str(tmp_path / 'a.wav'), 'rb')
    rate, data = whisper_tool.read_wav_f32(str(tmp_path / 'a.wav'))
    assert rate == 24000 and data.dtype == np.float32
    assert list(data) == SAMPLES


@pytest.mark.parametrize('fmt', ['WAV', 'WAVEX'])
def test_soundfiles_float_wav_is_read_plain_or_extensible(tmp_path, fmt):
    sf = pytest.importorskip('soundfile')
    sf.write(str(tmp_path / 'a.wav'), np.array(SAMPLES, dtype='float32'),
             22050, format=fmt, subtype='FLOAT')
    rate, data = whisper_tool.read_wav_f32(str(tmp_path / 'a.wav'))
    assert rate == 22050
    assert list(data) == SAMPLES


def test_a_float_stereo_wav_is_mixed_to_mono(tmp_path):
    import scipy.io.wavfile
    stereo = np.array([[0.5, -0.5], [1.0, 0.0], [0.25, 0.75]], dtype='float32')
    scipy.io.wavfile.write(str(tmp_path / 'a.wav'), 24000, stereo)
    _, data = whisper_tool.read_wav_f32(str(tmp_path / 'a.wav'))
    assert list(data) == [0.0, 0.5, 0.5]


def test_a_float_wav_cut_off_mid_frame_reads_its_whole_frames(tmp_path):
    import scipy.io.wavfile
    scipy.io.wavfile.write(str(tmp_path / 'a.wav'), 24000,
                           np.array(SAMPLES, dtype='float32'))
    blob = (tmp_path / 'a.wav').read_bytes()
    (tmp_path / 'b.wav').write_bytes(blob[:-2])  # half of the last sample
    _, data = whisper_tool.read_wav_f32(str(tmp_path / 'b.wav'))
    assert list(data) == SAMPLES[:-1]


def test_a_file_that_is_not_a_float_wav_says_what_it_is(tmp_path):
    (tmp_path / 'a.wav').write_bytes(b'not audio at all')
    with pytest.raises(ValueError, match='not a WAV file'):
        whisper_tool.read_wav_f32(str(tmp_path / 'a.wav'))
    # A WAV in a format neither reader decodes: A-law (tag 6).
    fmt = struct.pack('<HHIIHH', 6, 1, 8000, 8000, 1, 8)
    data = b'\x55' * 8
    body = b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt + \
        b'data' + struct.pack('<I', len(data)) + data
    (tmp_path / 'b.wav').write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)
    with pytest.raises(ValueError, match='unsupported WAV format 6'):
        whisper_tool.read_wav_f32(str(tmp_path / 'b.wav'))
