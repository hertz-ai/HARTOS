"""select_whisper_model prefers an STT model already on disk.

ModelCatalog.select_best only gives "downloaded" a scoring bonus, so a large
not-yet-downloaded model can outrank a small cached one; picking it starts a
multi-GB download inside the request's timeout window (found 2026-09-25).
"""
import sys
import types

import pytest

import integrations.service_tools.whisper_tool as wt


@pytest.fixture
def catalog_picks(monkeypatch):
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    big = next(k for k in wt._SHERPA_MODELS if k not in ('moonshine-tiny', 'whisper-tiny'))
    monkeypatch.setattr(wt, '_catalog_stt_entry', lambda: types.SimpleNamespace(id='stt-test-big'))
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-test-big', big)

    def cached(*names):
        monkeypatch.setattr(wt, '_sherpa_model_cached', lambda key: key in names)
    return big, cached


def test_cached_catalog_pick_is_used(catalog_picks):
    big, cached = catalog_picks
    cached(big, 'moonshine-tiny')
    assert wt.select_whisper_model() == big


def test_uncached_pick_falls_back_to_a_cached_small_model(catalog_picks):
    big, cached = catalog_picks
    cached('moonshine-tiny')
    assert wt.select_whisper_model() == 'moonshine-tiny'


def test_nothing_cached_keeps_the_catalog_pick(catalog_picks):
    big, cached = catalog_picks
    cached()
    assert wt.select_whisper_model() == big


def test_sherpa_model_cached_reads_the_real_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, '_get_stt_dir', lambda: tmp_path)
    cfg = wt._SHERPA_MODELS['moonshine-tiny']
    assert wt._sherpa_model_cached('moonshine-tiny') is False
    d = tmp_path / cfg['dir']
    d.mkdir(parents=True)
    (d / cfg['files']['tokens']).write_text('x')
    assert wt._sherpa_model_cached('moonshine-tiny') is True
    assert wt._sherpa_model_cached('no-such-model') is False
