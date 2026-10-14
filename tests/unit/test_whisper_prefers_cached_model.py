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
        state['cached'] = set(names)
    state = {'cached': set(), 'downloads': []}
    monkeypatch.setattr(wt, '_sherpa_model_cached',
                        lambda key: key in state['cached'])

    # The network fetch is the boundary: record it, and "finish" it by
    # putting the model on disk.  The thread runs inline so the test sees
    # it complete.
    def download(name):
        state['downloads'].append(name)
        state['cached'].add(name)
    monkeypatch.setattr(wt, '_download_model', download)

    class _Inline:
        def __init__(self, target=None, **_kw):
            self._target = target

        def start(self):
            self._target()
    monkeypatch.setattr(wt.threading, 'Thread', _Inline)
    cached.state = state
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


def test_a_fallback_fetches_the_pick_so_the_node_is_not_stuck_on_it(catalog):
    """Serving the cached model must not be permanent: the catalog's pick
    is fetched in the background (once), and the next selection uses it.
    Before, nothing downloaded it, since _download_model only ran for the
    model actually chosen, so a node that once cached a tiny model kept it."""
    (big, mid, small), cached = catalog
    cached(small)
    assert wt.select_whisper_model() == small       # answers now
    assert cached.state['downloads'] == [big]      # fetches the pick
    assert wt.select_whisper_model() == big         # and uses it next time


def test_a_running_download_is_not_started_twice(catalog, monkeypatch):
    (big, mid, small), cached = catalog
    cached(small)
    monkeypatch.setattr(wt, '_background_downloads', {big})
    assert wt.select_whisper_model() == small
    assert cached.state['downloads'] == []


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
    # tokens alone is a cut-short extraction, not a model
    assert wt._sherpa_model_cached('moonshine-tiny') is False
    for f in cfg['files'].values():
        (d / f).write_text('x')
    assert wt._sherpa_model_cached('moonshine-tiny') is True
    assert wt._sherpa_model_cached('no-such-model') is False


def _archive_bytes(tmp_path, cfg, files=None):
    """A real .tar.bz2 shaped like sherpa's release: one top folder."""
    import tarfile
    src = tmp_path / 'src' / cfg['dir']
    src.mkdir(parents=True)
    for f in (cfg['files'].values() if files is None else files):
        (src / f).write_text('w')
    out = tmp_path / 'a.tar.bz2'
    with tarfile.open(out, 'w:bz2') as tar:
        tar.add(src, arcname=cfg['dir'])
    return out.read_bytes()


def _fake_fetch(monkeypatch, data, calls=None):
    def fetch(url, path):
        if calls is not None:
            calls.append(url)
        with open(path, 'wb') as fh:
            fh.write(data)
    monkeypatch.setattr(wt.urllib.request, 'urlretrieve', fetch)


def test_download_lands_a_complete_folder_and_leaves_no_work_files(
        tmp_path, monkeypatch):
    stt = tmp_path / 'stt'
    stt.mkdir()
    monkeypatch.setattr(wt, '_get_stt_dir', lambda: stt)
    cfg = wt._SHERPA_MODELS['whisper-tiny']
    _fake_fetch(monkeypatch, _archive_bytes(tmp_path, cfg))

    assert wt._download_model('whisper-tiny') == stt / cfg['dir']
    assert wt._sherpa_model_cached('whisper-tiny')
    assert sorted(p.name for p in stt.iterdir()) == [cfg['dir']]


def test_a_short_archive_never_becomes_a_model(tmp_path, monkeypatch):
    stt = tmp_path / 'stt'
    stt.mkdir()
    monkeypatch.setattr(wt, '_get_stt_dir', lambda: stt)
    cfg = wt._SHERPA_MODELS['whisper-tiny']
    _fake_fetch(monkeypatch,
                _archive_bytes(tmp_path, cfg, files=[cfg['files']['tokens']]))

    with pytest.raises(RuntimeError):
        wt._download_model('whisper-tiny')
    assert list(stt.iterdir()) == []


def test_an_old_half_extracted_folder_is_replaced(tmp_path, monkeypatch):
    stt = tmp_path / 'stt'
    leftover = stt / wt._SHERPA_MODELS['whisper-tiny']['dir']
    leftover.mkdir(parents=True)
    cfg = wt._SHERPA_MODELS['whisper-tiny']
    (leftover / cfg['files']['tokens']).write_text('stale')
    monkeypatch.setattr(wt, '_get_stt_dir', lambda: stt)
    _fake_fetch(monkeypatch, _archive_bytes(tmp_path, cfg))

    wt._download_model('whisper-tiny')
    assert wt._sherpa_model_cached('whisper-tiny')
    assert (leftover / cfg['files']['tokens']).read_text() == 'w'


def test_a_failed_background_fetch_waits_before_retrying(catalog, monkeypatch):
    """Every STT request selects; an offline node must not restart a
    multi-GB fetch on each one."""
    (big, mid, small), cached = catalog
    cached(small)
    monkeypatch.setattr(wt, '_background_failures', {})
    attempts = []

    def failing(name):
        attempts.append(name)
        raise OSError('offline')
    monkeypatch.setattr(wt, '_download_model', failing)
    clock = [1000.0]
    monkeypatch.setattr(wt.time, 'monotonic', lambda: clock[0])

    assert wt.select_whisper_model() == small
    assert wt.select_whisper_model() == small
    assert attempts == [big]                      # waited, did not refetch
    clock[0] += wt._background_retry_wait(1) + 1
    assert wt.select_whisper_model() == small
    assert attempts == [big, big]                 # retried after the wait
    clock[0] += wt._background_retry_wait(1) + 1  # less than the doubled wait
    wt.select_whisper_model()
    assert attempts == [big, big]


def test_concurrent_selections_start_one_fetch(monkeypatch):
    import threading as _t
    real_thread = _t.Thread  # captured before the module's Thread is held
    big = list(wt._SHERPA_MODELS)[0]
    monkeypatch.setattr(wt, '_background_failures', {})
    monkeypatch.setattr(wt, '_background_downloads', set())
    started = []

    class _Held:  # a thread that never finishes during the test
        def __init__(self, target=None, **_kw):
            pass

        def start(self):
            started.append(1)
    monkeypatch.setattr(wt.threading, 'Thread', _Held)
    barrier = _t.Barrier(8)
    results = []

    def go():
        barrier.wait()
        results.append(wt._download_in_background(big))
    workers = [real_thread(target=go) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert results.count(True) == 1
    assert started == [1]


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
    # The uncached pick would start a REAL fetch into ~/.hevolve/models/stt.
    monkeypatch.setattr(wt, '_download_in_background', lambda name: False)
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
