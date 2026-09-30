"""A faster-whisper model whose decode raised is never decoded on again.

Live 2026-09-25/26 (gui_app.log, Nunba on an RTX 3070 box): the STT worker
loaded faster-whisper on cuda, the first decode raised
"Library cublas64_12.dll is not found or cannot be loaded", and the NEXT
request went to the same cached model.  ctranslate2 never returns from a
second encode on a model whose cuBLAS load failed (measured on the installed
python-embed with ctranslate2 4.8.2: the second call hung until timeout
killed it), so the single-threaded worker sat 180 s until gpu_worker killed
it, every streaming window queued behind it came back '', and the respawn
repeated the cycle.

Measured on the same interpreter: dropping that model and loading the CPU
int8 rung in the SAME process decodes normally.  So the rule is:
  * a cuda decode that raises is a cuda failure, exactly like a cuda load
    that raises -- CUDA is off for the rest of this process and the request
    is answered on the CPU rung;
  * a model whose decode raised is dropped from the cache (either device),
    so the cache check cannot hand it out again.

Behavioural: the real loader and the real transcribe / language-detect paths
against fake faster_whisper + ctranslate2 modules (the only boundary mocked)
and a recording orchestrator.

    python -m pytest tests/unit/test_stt_cuda_runtime_fallback.py --noconftest -q
"""
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest

import integrations.service_tools.whisper_tool as wt

CUBLAS_ERR = "Library cublas64_12.dll is not found or cannot be loaded"


class _Engine:
    """Fake faster_whisper + ctranslate2 pair.  Every WhisperModel instance
    is recorded; ``cuda_decode_fails`` / ``cpu_decode_fails`` script how many
    decodes on that device raise before one succeeds."""

    def __init__(self, cuda_decode_fails=0, cpu_decode_fails=0,
                 cuda_load_fails=0, failing_cpu_sizes=(),
                 cuda_error=CUBLAS_ERR):
        self.models = []
        self.attempts = []          # every WhisperModel() call, raised or not
        self.fail_left = {'cuda': cuda_decode_fails, 'cpu': cpu_decode_fails}
        self.cuda_load_fails_left = cuda_load_fails
        engine = self

        class WhisperModel:
            def __init__(self, size, device='cpu', compute_type='int8'):
                engine.attempts.append((size, device))
                if device == 'cuda' and engine.cuda_load_fails_left > 0:
                    engine.cuda_load_fails_left -= 1
                    raise RuntimeError('CUDA failed with error out of memory')
                if device == 'cpu' and size in failing_cpu_sizes:
                    raise RuntimeError(f'no model files for {size}')
                self.size, self.device, self.compute_type = size, device, compute_type
                self.decodes = 0
                engine.models.append(self)

            def transcribe(self, audio_path, **kw):
                self.decodes += 1
                if engine.fail_left[self.device] > 0:
                    engine.fail_left[self.device] -= 1
                    raise RuntimeError(cuda_error if self.device == 'cuda'
                                       else 'decode failed')
                seg = SimpleNamespace(text=f'hello from {self.device}',
                                      no_speech_prob=0.01, avg_logprob=-0.2)
                info = SimpleNamespace(language='en', language_probability=0.97)
                return iter([seg]), info

        self.fw = types.ModuleType('faster_whisper')
        self.fw.WhisperModel = WhisperModel
        self.ct = types.ModuleType('ctranslate2')
        self.ct.get_cuda_device_count = lambda: 1
        self.ct.get_supported_compute_types = (
            lambda dev: {'int8', 'int8_float16', 'float16', 'float32'})

    def loads(self):
        return [(m.size, m.device) for m in self.models]


class _Orch:
    def __init__(self):
        self.events = []

    def notify_loaded(self, model_type, name, device='gpu', vram_gb=0):
        self.events.append(('loaded', model_type, name, device))

    def notify_unloaded(self, model_type, name):
        self.events.append(('unloaded', model_type, name))


@pytest.fixture
def orch(monkeypatch):
    o = _Orch()
    monkeypatch.setattr(
        'integrations.service_tools.model_orchestrator.get_orchestrator',
        lambda: o)
    return o


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch):
    # No model cached, CUDA not yet known-bad, and the retry gates out of the
    # way (their own contract is pinned by tests/test_whisper_backoff.py).
    for name in ('_faster_whisper_model', '_faster_whisper_model_size',
                 '_faster_whisper_model_device',
                 '_faster_whisper_model_loaded_size',
                 '_faster_whisper_cuda_error',
                 '_whisper_load_breaker', '_whisper_load_backoff',
                 '_whisper_last_error'):
        monkeypatch.setattr(wt, name, None)


def _modules(engine):
    return mock.patch.dict(sys.modules, {'faster_whisper': engine.fw,
                                         'ctranslate2': engine.ct})


def test_a_cuda_decode_failure_is_answered_on_the_cpu_rung(orch):
    eng = _Engine(cuda_decode_fails=1)
    with _modules(eng):
        out = wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    assert out is not None, 'the request that hit the cuBLAS error got no text'
    assert json.loads(out)['text'] == 'hello from cpu'
    assert eng.loads() == [('medium', 'cuda'), (wt.STT_CPU_MODEL_SIZE, 'cpu')]
    assert eng.models[1].compute_type == 'int8'
    # Recovered, so nothing is left in the user-visible error slot.
    assert wt.get_whisper_last_error() is None


def test_the_cuda_model_that_raised_is_never_decoded_on_again(orch):
    eng = _Engine(cuda_decode_fails=1)
    with _modules(eng):
        for _ in range(3):
            wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    cuda = eng.models[0]
    assert cuda.device == 'cuda'
    assert cuda.decodes == 1, 'a second encode on this model hangs the worker'
    # One CPU model, loaded once and reused: no reload per request.
    assert eng.loads() == [('medium', 'cuda'), (wt.STT_CPU_MODEL_SIZE, 'cpu')]
    assert eng.models[1].decodes == 3


def test_cuda_stays_off_for_the_process_after_a_decode_failure(orch):
    eng = _Engine(cuda_decode_fails=1)
    with _modules(eng):
        wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
        # Whatever empties the cache (an unload, a size change), the next
        # load must not walk back into the cuda build that cannot decode.
        wt._faster_whisper_model = None
        wt._faster_whisper_model_size = None
        wt._faster_whisper_transcribe('a.wav', None, model_size='small')
    assert [d for (_s, d) in eng.loads()] == ['cuda', 'cpu', 'cpu']


def test_a_cpu_model_whose_decode_raised_is_not_reused(orch):
    eng = _Engine(cpu_decode_fails=1)
    eng.ct.get_cuda_device_count = lambda: 0          # a CPU-only box
    with _modules(eng):
        first = wt._faster_whisper_transcribe('a.wav', None, model_size='base')
        second = wt._faster_whisper_transcribe('a.wav', None, model_size='base')
    assert first is None
    assert json.loads(second)['text'] == 'hello from cpu'
    assert eng.loads() == [('base', 'cpu'), ('base', 'cpu')]
    assert [m.decodes for m in eng.models] == [1, 1]


def test_language_detection_takes_the_same_fallback(orch):
    eng = _Engine(cuda_decode_fails=1)
    with _modules(eng):
        out = json.loads(wt._detect_language_impl('a.wav', model_size='medium'))
        wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    assert out == {'language': 'en', 'probability': 0.97}
    assert eng.models[0].decodes == 1
    assert [d for (_s, d) in eng.loads()] == ['cuda', 'cpu']


def test_the_dropped_cuda_model_gives_back_its_booking(orch):
    eng = _Engine(cuda_decode_fails=1)
    with _modules(eng):
        wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    assert orch.events == [
        ('loaded', 'stt', 'whisper-medium', 'gpu'),
        ('unloaded', 'stt', 'whisper-medium'),
        ('loaded', 'stt', f'whisper-{wt.STT_CPU_MODEL_SIZE}', 'cpu'),
    ]


def test_a_failure_on_the_cpu_rung_too_is_recorded_and_returns_none(orch):
    eng = _Engine(cuda_decode_fails=1, cpu_decode_fails=1)
    with _modules(eng):
        out = wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    assert out is None
    assert 'decode failed' in (wt.get_whisper_last_error() or '')
    # Neither model that raised stays cached.
    assert wt._faster_whisper_model is None


def test_a_cuda_load_failure_keeps_cuda_off_after_the_cache_empties(orch):
    # A cuda LOAD that raised switches cuda off for the process, like a cuda
    # decode that raised: whatever empties the cache later (a drop, a size
    # change) must load the CPU rung, not walk back into the failing load.
    eng = _Engine(cuda_load_fails=1)
    with _modules(eng):
        first = wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
        wt._drop_faster_whisper_model()
        second = wt._faster_whisper_transcribe('a.wav', None, model_size='small')
    assert json.loads(first)['text'] == 'hello from cpu'
    assert json.loads(second)['text'] == 'hello from cpu'
    assert eng.attempts == [('medium', 'cuda'),
                            (wt.STT_CPU_MODEL_SIZE, 'cpu'),
                            (wt.STT_CPU_MODEL_SIZE, 'cpu')]


def test_a_decode_on_the_cached_model_clears_an_earlier_failure(orch, monkeypatch):
    # The cached model answers without a load, so the transcribe itself must
    # clear what an earlier failure left: the user-visible error and the
    # backoff that would refuse the next load.
    from core.circuit_breaker import PeerBackoff
    backoff = PeerBackoff(initial=60.0, maximum=300.0)
    monkeypatch.setattr(wt, '_whisper_load_backoff', backoff)
    eng = _Engine(failing_cpu_sizes=('small',))
    eng.ct.get_cuda_device_count = lambda: 0          # a CPU-only box
    with _modules(eng):
        assert wt._faster_whisper_transcribe('a.wav', None, model_size='base')
        assert wt._faster_whisper_transcribe('a.wav', None, model_size='small') is None
        assert 'no model files for small' in (wt.get_whisper_last_error() or '')
        assert backoff.is_backed_off('faster_whisper')
        out = wt._faster_whisper_transcribe('a.wav', None, model_size='base')
    assert json.loads(out)['text'] == 'hello from cpu'
    assert eng.attempts == [('base', 'cpu'), ('small', 'cpu')], 'base came from the cache'
    assert wt.get_whisper_last_error() is None
    assert not backoff.is_backed_off('faster_whisper')


# ── What the worker logs when ctranslate2 cannot load a CUDA library ────────
# The live boxes' failure was "Library cublas64_12.dll is not found or cannot
# be loaded" while torch/lib, which carries that DLL, was on the worker's PATH
# (review measurement, 2026-09-28).  Why it did not resolve was never
# measured, so a cuda decode that names a library logs what THIS process gets
# when it loads that library itself, and the search path it used.

def _cuda_decode_warnings(caplog, error):
    eng = _Engine(cuda_decode_fails=1, cuda_error=error)
    with caplog.at_level('WARNING', logger=wt.logger.name), _modules(eng):
        out = wt._faster_whisper_transcribe('a.wav', None, model_size='medium')
    assert json.loads(out)['text'] == 'hello from cpu'
    return [r.getMessage() for r in caplog.records
            if 'cuda library' in r.getMessage()]


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows DLL search')
def test_a_library_found_on_path_is_logged_with_the_file_it_resolved_to(
        caplog, monkeypatch, tmp_path):
    # A DLL that exists ONLY in a directory on PATH: the probe must search
    # PATH the way ctranslate2's LoadLibrary does, and name the file.
    import shutil
    lib = tmp_path / 'hart_cuda_probe_copy.dll'
    shutil.copyfile(os.path.join(os.environ['SystemRoot'], 'System32', 'version.dll'), lib)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    lines = _cuda_decode_warnings(
        caplog, 'Library hart_cuda_probe_copy.dll is not found or cannot be loaded')
    assert len(lines) == 1
    msg = lines[0]
    assert 'hart_cuda_probe_copy.dll loads in this process from ' in msg
    resolved = msg.split(' from ', 1)[1].split(';', 1)[0]
    assert os.path.normcase(resolved) == os.path.normcase(str(lib))
    assert f"PATH={os.environ['PATH']}" in msg


def test_a_library_that_does_not_load_here_either_is_logged_with_why(caplog):
    lines = _cuda_decode_warnings(
        caplog, 'Library hart_no_such_cuda_lib_12.dll is not found or cannot be loaded')
    assert len(lines) == 1
    msg = lines[0]
    assert 'hart_no_such_cuda_lib_12.dll does not load in this process either: ' in msg
    var = 'PATH' if sys.platform == 'win32' else 'LD_LIBRARY_PATH'
    assert f"{var}={os.environ.get(var, '')}" in msg


def test_a_cuda_error_that_names_no_library_logs_no_library_report(caplog):
    assert _cuda_decode_warnings(caplog, 'CUDA failed with error out of memory') == []


def test_a_probe_that_itself_fails_still_leaves_the_cpu_answer(caplog, monkeypatch):
    import ctypes

    def _broken_loader(*a, **kw):
        raise ValueError('loader exploded')
    monkeypatch.setattr(ctypes, 'CDLL', _broken_loader)
    lines = _cuda_decode_warnings(caplog, 'Library cublas64_12.dll is not found or cannot be loaded')
    assert len(lines) == 1
    assert 'cublas64_12.dll could not be probed (ValueError: loader exploded)' in lines[0]
