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
def catalog(monkeypatch):
    """A ranked fake catalog: select_best('stt', exclude=...) returns the best
    entry not excluded, like the real one.  Ranking: big > mid > small."""
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    keys = list(wt._SHERPA_MODELS)[:3]
    ranking = ['stt-test-big', 'stt-test-mid', 'stt-test-small']
    for cid, key in zip(ranking, keys):
        monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, cid, key)

    def select(exclude=None):
        for cid in ranking:
            if cid not in (exclude or ()):
                return types.SimpleNamespace(id=cid)
        return None
    monkeypatch.setattr(wt, '_catalog_stt_entry', select)

    def cached(*names):
        monkeypatch.setattr(wt, '_sherpa_model_cached', lambda key: key in names)
    return keys, cached


def test_cached_catalog_pick_is_used(catalog):
    (big, mid, small), cached = catalog
    cached(big, small)
    assert wt.select_whisper_model() == big


def test_uncached_pick_falls_back_to_the_next_cached_in_catalog_order(catalog):
    (big, mid, small), cached = catalog
    cached(mid, small)
    assert wt.select_whisper_model() == mid


def test_fallback_skips_uncached_entries(catalog):
    (big, mid, small), cached = catalog
    cached(small)
    assert wt.select_whisper_model() == small


def test_nothing_cached_keeps_the_catalog_pick(catalog):
    (big, mid, small), cached = catalog
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


def test_a_cached_model_ranked_below_many_other_engines_is_still_found(monkeypatch):
    """faster-whisper entries share the ranking; a cached sherpa model ranked
    below more of them than there are sherpa models must still be found."""
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    big, small = list(wt._SHERPA_MODELS)[:2]
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-t-big', big)
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-t-small', small)
    others = [f'stt-t-fw-{i}' for i in range(len(wt._CATALOG_ID_TO_SHERPA) + 2)]
    ranking = ['stt-t-big', *others, 'stt-t-small']
    monkeypatch.setattr(wt, '_catalog_stt_entry', lambda exclude=None: next(
        (types.SimpleNamespace(id=c) for c in ranking if c not in (exclude or ())),
        None))
    monkeypatch.setattr(wt, '_sherpa_model_cached', lambda key: key == small)
    assert wt.select_whisper_model() == small


def test_a_catalog_that_ignores_exclude_stops_instead_of_looping(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', types.ModuleType('sherpa_onnx'))
    big = list(wt._SHERPA_MODELS)[0]
    monkeypatch.setitem(wt._CATALOG_ID_TO_SHERPA, 'stt-t-big', big)
    monkeypatch.setattr(wt, '_catalog_stt_entry',
                        lambda exclude=None: types.SimpleNamespace(id='stt-t-big'))
    monkeypatch.setattr(wt, '_sherpa_model_cached', lambda key: False)
    assert wt.select_whisper_model() == big   # keeps the catalog pick
    assert any('although it was excluded' in r.getMessage() for r in caplog.records)
