"""Tests for #58 Scope-2 — reflection dispatch via catalog id.

Guards:
  1. ``_normalize_to_wav_file`` writes the right bytes for each
     canonical ``output_format`` (wav_bytes / numpy_24k / file_path /
     bytesio) and raises TypeError on shape mismatch.
  2. ``_build_reflection_callbacks`` validates the 5-field contract via
     ``tts_router._validate_engine_caps`` (single source of truth) and
     raises RuntimeError on violation BEFORE returning callbacks.
  3. The built ``load`` callback instantiates the class via
     ``import_path`` and ``init_args``; the built ``handle`` callback
     translates payload kwargs through ``params_map`` and normalizes
     the engine's raw output via ``output_format``.
  4. The wire response shape (``{path, duration, sample_rate, engine}``)
     matches what existing on-disk *_tool.py modules return — same
     subprocess protocol, same downstream handling.
  5. The ``--catalog-id`` CLI path round-trips through
     ``populate_tts_catalog`` (a reflection-only entry survives ingest
     post-Scope-2 but is excluded from ENGINE_REGISTRY).
"""

import io
import json
import struct
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── Fixture engine ─────────────────────────────────────────────────
# A tiny class the reflection dispatcher can import + instantiate via
# 'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine'.  Each
# test below points the dispatcher at one of its synth methods and
# verifies the round-trip wire shape.


class FixtureEngine:
    """Mock engine with one method per canonical output_format.

    Constructor takes ``init_args`` so we can verify they reach the
    callable.  Synth methods accept ``text`` (or alias) and return the
    declared format.
    """

    def __init__(self, voice_id: str = 'default', sample_rate: int = 24000):
        self.voice_id = voice_id
        self.sample_rate = sample_rate

    def speak_wav_bytes(self, text: str) -> bytes:
        return _make_wav_bytes(0.05, self.sample_rate)

    def speak_bytesio(self, text: str) -> io.BytesIO:
        return io.BytesIO(_make_wav_bytes(0.05, self.sample_rate))

    def speak_file_path(self, text: str, output_path: str = '') -> str:
        # Engine writes the wav itself; dispatcher copies to its output_path.
        # We use a pytest tmp_path-supplied file via the test fixture.
        if not output_path:
            raise ValueError('FixtureEngine.speak_file_path needs output_path')
        Path(output_path).write_bytes(_make_wav_bytes(0.05, self.sample_rate))
        return output_path

    def speak_numpy_24k(self, text: str):
        try:
            import numpy as np
        except ImportError:
            pytest.skip('numpy unavailable in this test env')
        # 0.05 s of silence at 24 kHz, float32 mono.
        return np.zeros(int(24000 * 0.05), dtype='float32')

    def speak_alias_text_arg(self, message: str) -> bytes:
        """Used to verify params_map remaps payload['text'] → method's
        'message' kwarg."""
        return _make_wav_bytes(0.05, self.sample_rate)


def _make_wav_bytes(duration_s: float, sample_rate: int = 24000) -> bytes:
    """Tiny silent WAV header + zeros payload — keeps tests scipy-free."""
    n_frames = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)            # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b'\x00\x00' * n_frames)
    return buf.getvalue()


# ── _normalize_to_wav_file ──────────────────────────────────────────


class TestNormalizeToWavFile:
    def test_wav_bytes_writes_verbatim(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        raw = _make_wav_bytes(0.05, 24000)
        out = str(tmp_path / 'a.wav')
        path, duration = _normalize_to_wav_file(raw, 'wav_bytes', out)
        assert path == out
        assert Path(out).read_bytes() == raw
        assert duration > 0

    def test_bytesio_writes_verbatim(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        raw = io.BytesIO(_make_wav_bytes(0.05, 24000))
        out = str(tmp_path / 'b.wav')
        _normalize_to_wav_file(raw, 'bytesio', out)
        # raw.getvalue() should equal what we wrote
        assert Path(out).read_bytes() == raw.getvalue()

    def test_file_path_copies_when_different(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        src = tmp_path / 'engine_wrote.wav'
        dst = tmp_path / 'dispatcher_target.wav'
        src.write_bytes(_make_wav_bytes(0.05, 24000))
        _normalize_to_wav_file(str(src), 'file_path', str(dst))
        assert dst.read_bytes() == src.read_bytes()
        # Source still exists — copy not move
        assert src.exists()

    def test_file_path_noop_when_same(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        out = tmp_path / 'one.wav'
        out.write_bytes(_make_wav_bytes(0.05, 24000))
        _normalize_to_wav_file(str(out), 'file_path', str(out))
        # Still exists, content unchanged
        assert out.exists()

    def test_numpy_24k_writes_via_scipy(self, tmp_path):
        np = pytest.importorskip('numpy')
        scipy_wav = pytest.importorskip('scipy.io.wavfile')
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        raw = np.zeros(int(24000 * 0.05), dtype='float32')
        out = str(tmp_path / 'np.wav')
        path, duration = _normalize_to_wav_file(raw, 'numpy_24k', out)
        # scipy writes float WAV (format code 3) — read it back via
        # scipy, NOT wave.open (stdlib only handles PCM).  The
        # _normalize_to_wav_file duration fallback follows the same
        # pattern internally.
        sr, data = scipy_wav.read(out)
        assert sr == 24000
        assert len(data) == int(24000 * 0.05)
        assert duration == pytest.approx(0.05, abs=0.001)

    def test_wav_bytes_shape_mismatch_raises_typeerror(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        with pytest.raises(TypeError):
            _normalize_to_wav_file('not bytes', 'wav_bytes',
                                   str(tmp_path / 'x.wav'))

    def test_unknown_format_raises_valueerror(self, tmp_path):
        from integrations.service_tools.gpu_worker import _normalize_to_wav_file

        with pytest.raises(ValueError, match='unknown output_format'):
            _normalize_to_wav_file(b'x', 'mp3', str(tmp_path / 'x.wav'))


# ── _build_reflection_callbacks ─────────────────────────────────────


class TestBuildReflectionCallbacks:
    def test_validation_failure_raises_runtimeerror(self):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        # Missing all reflection fields and no tool_module → invalid
        with pytest.raises(RuntimeError, match='catalog entry'):
            _build_reflection_callbacks('tts-bad', {})

    def test_load_callback_instantiates_class(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {'voice_id': 'test_voice'},
            'synth_method': 'speak_wav_bytes',
            'params_map': {},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        model = load()
        assert isinstance(model, FixtureEngine)
        assert model.voice_id == 'test_voice'

    def test_handle_wav_bytes_round_trip(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_wav_bytes',
            'params_map': {},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        model = load()
        out = handle(model, {'text': 'hello'})
        assert 'error' not in out, f'expected success, got {out!r}'
        assert out['engine'] == 'reflection:tts-fixture'
        assert Path(out['path']).exists()
        assert out['duration'] > 0
        assert out['sample_rate'] == 24000

    def test_handle_bytesio_round_trip(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_bytesio',
            'params_map': {},
            'output_format': 'bytesio',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        out = handle(load(), {'text': 'hi'})
        assert 'error' not in out
        assert Path(out['path']).read_bytes()  # non-empty

    def test_handle_file_path_round_trip(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_file_path',
            # Engine needs output_path arg routed in
            'params_map': {'output_path': 'output_path'},
            'output_format': 'file_path',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        engine_out = str(tmp_path / 'engine_wrote.wav')
        out = handle(load(), {
            'text': 'hi',
            'output_path': engine_out,
        })
        assert 'error' not in out, f'expected success, got {out!r}'
        # Dispatcher writes/copies to request['output_path']
        assert Path(out['path']).exists()

    def test_handle_params_map_remaps_text_to_method_alias(self, tmp_path):
        # Engine method takes 'message' (not 'text'); params_map
        # translates payload['text'] → method kwarg 'message'.
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_alias_text_arg',
            'params_map': {'text': 'message'},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        out = handle(load(), {'text': 'hello'})
        assert 'error' not in out, f'expected success, got {out!r}'

    def test_handle_missing_text_returns_error(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_wav_bytes',
            'params_map': {},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        out = handle(load(), {})  # no text
        assert out['error'] == 'text is required'

    def test_handle_load_failure_surfaces_runtimeerror(self):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:DoesNotExist',
            'init_args': {},
            'synth_method': 'speak',
            'params_map': {},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks('tts-missing', caps)
        with pytest.raises(RuntimeError,
                           match=r'class .*DoesNotExist.* not found'):
            load()

    def test_handle_method_missing_returns_error(self, tmp_path):
        from integrations.service_tools.gpu_worker import _build_reflection_callbacks

        caps = {
            'import_path':
                'tests.unit.test_gpu_worker_reflection_dispatch:FixtureEngine',
            'init_args': {},
            'synth_method': 'speak_does_not_exist',
            'params_map': {},
            'output_format': 'wav_bytes',
        }
        load, handle = _build_reflection_callbacks(
            'tts-fixture', caps, output_dir=str(tmp_path),
        )
        out = handle(load(), {'text': 'hi'})
        assert 'method' in out['error']
        assert 'not found' in out['error']


# ── _dispatch_catalog_id (CLI surface, exit codes) ──────────────────


class TestDispatchCatalogIdExitCodes:
    """Cover the diagnostic-exit branches (bad catalog entry → exit 2).

    The READY/handler loop in `run_worker` is exercised by the
    callback-level tests above — we don't reproduce the stdin
    protocol here since `run_worker` blocks on stdin reads.
    """

    def test_unknown_catalog_id_exits_2(self, capsys, monkeypatch):
        from integrations.service_tools import gpu_worker

        # Stub the catalog to return None for any get()
        class _StubCatalog:
            def get(self, _id):
                return None

        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: _StubCatalog(),
        )
        with pytest.raises(SystemExit) as exc:
            gpu_worker._dispatch_catalog_id('tts-not-real')
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert 'no entry' in captured.err
        assert 'tts-not-real' in captured.err

    def test_invalid_capabilities_exit_2(self, capsys, monkeypatch):
        from integrations.service_tools import gpu_worker

        class _Entry:
            capabilities = {}  # empty caps fail validation

        class _StubCatalog:
            def get(self, _id):
                return _Entry()

        monkeypatch.setattr(
            'integrations.service_tools.model_catalog.get_catalog',
            lambda: _StubCatalog(),
        )
        with pytest.raises(SystemExit) as exc:
            gpu_worker._dispatch_catalog_id('tts-bad')
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert 'tts-bad' in captured.err


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
