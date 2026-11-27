"""The voiced memo and the warm-up keep their word (review rv_b6 of HARTOS 439fd431b, F1 and F2;
review rv_tts of the fix).

F1, the memo (tts_router._remembered / _remember / _memo_dir):
  * a remembered reply is ONE file, handed to every caller that repeats it -- not a new copy per
    call (measured on 439fd431b: 1 index entry and 101 WAVs after 100 repeats).  The callers only
    read, play or serve what they get, none changes it;
  * nothing here deletes a remembered file: not a process's first use (the old first use deleted
    every *.wav, also files another process had handed out), not a dropped entry.  The folder's ONE
    deleter is core.spoken_media's pruner (rv_tts: a sweep here was a second deleter); each hand-out
    refreshes the file's time so a file in use is the last it removes, and a file it removed is
    spoken again.

F2, the warm-up (TTSRouter._warm_up): its WARM_UP_EVERY_S throttle limits repeated ATTEMPTS -- one
that failed, one in flight, or an answer that left no engine running.  When the ladder's head came
up and later stopped (idle, evicted), it is brought up again after WARM_UP_FLOOR_S, not 10 minutes
later and not on every reply; a lower engine coming up never lifts the head's throttle (rv_tts R3).
"""
import os
import sys
import threading
import time
import types
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.unit.module_swap import swap_modules  # noqa: E402

from integrations.channels.media import tts_router as tr  # noqa: E402
from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402


class _Worker:
    """A ToolWorker whose liveness the test sets: an engine starts, idles out, is evicted."""

    def __init__(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def stop(self):
        self.alive = False


def _join_warm_ups():
    for t in threading.enumerate():
        if t.name == 'tts-warm-up':
            t.join(5)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """A memo index, memo folder and warm-up throttle of this test's own; the models root is
    tmp_path, so the real ~/.hevolve is never read or cleared."""
    monkeypatch.setattr(tr, '_voiced_memo', OrderedDict())
    monkeypatch.setattr(tr, '_voiced_memo_dir', {})
    monkeypatch.setattr(tr, '_warmed_at', {})
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'models'))
    monkeypatch.setattr(tn, 'normalize_for_tts', lambda t, *a, **k: t)
    monkeypatch.setattr(tr, '_audit_speech', lambda *a, **k: None)
    yield
    _join_warm_ups()


@pytest.fixture
def workers():
    """Fake tool modules for every engine row, each worker one the test starts and stops;
    nothing real is imported or started."""
    mods, by_engine = {}, {}
    for eid, seed in tr._SEED_SPECS.items():
        if not seed.tool_module or not seed.tool_worker_attr:
            continue
        mod = mods.setdefault(seed.tool_module, types.ModuleType(seed.tool_module))
        worker = getattr(mod, seed.tool_worker_attr, None) or _Worker()
        setattr(mod, seed.tool_worker_attr, worker)
        by_engine[eid] = worker
    pocket = types.ModuleType('integrations.service_tools.pocket_tts_tool')
    pocket.is_loaded = lambda: False
    mods['integrations.service_tools.pocket_tts_tool'] = pocket
    with swap_modules(mods):
        yield by_engine


@pytest.fixture
def files(tmp_path):
    voice = tmp_path / 'voice.wav'
    voice.write_bytes(b'RIFF-recording-1')
    out = tmp_path / 'cloned.wav'
    out.write_bytes(b'RIFF-cloned')
    return types.SimpleNamespace(voice=str(voice), out=str(out))


def _cand(eid):
    spec = tr.ENGINE_REGISTRY[eid]
    return tr.TTSCandidate(engine=spec, location=tr.TTSLocation.LOCAL, device='gpu',
                           estimated_latency_ms=100, quality_score=spec.quality)


def _router(ladder, execute):
    router = tr.TTSRouter()
    router.select_engines = MagicMock(return_value=[_cand(e) for e in ladder])
    router._execute = MagicMock(side_effect=execute)
    return router


def _speaks(files):
    return lambda cand, text, lang, voice, out: {
        'path': files.out, 'duration': 1.0, 'sample_rate': 24000, 'voice': voice}


def _say(router, text, files):
    return router.synthesize(text, language='en', voice=files.voice, running_only=True)


def _memo_files():
    folder = tr._memo_dir()
    return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith('.wav'))


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


# ── F1: one file per remembered reply ─────────────────────────────────────────

def test_a_hundred_repeats_leave_one_remembered_file(files, workers):
    workers['f5_tts'].alive = True
    router = _router(['f5_tts'], _speaks(files))
    _say(router, 'Great job!', files)
    handed = set()
    for _ in range(100):
        again = _say(router, 'Great job!', files)
        assert 'remembered' in again.warnings
        handed.add(again.path)
    assert router._execute.call_count == 1, 'every repeat came from memory'
    assert len(_memo_files()) == 1, 'one file per remembered reply, never one per call'
    assert handed == set(_memo_files()), 'every repeat is handed the remembered file itself'
    with open(_memo_files()[0], 'rb') as fh:
        assert fh.read() == b'RIFF-cloned'


# ── F1: a first use never deletes what another process handed out ──────────────

def test_a_second_process_never_deletes_a_file_the_first_handed_out(files, workers, monkeypatch):
    workers['f5_tts'].alive = True
    first_process = _router(['f5_tts'], _speaks(files))
    _say(first_process, 'Great job!', files)
    handed = _say(first_process, 'Great job!', files).path
    assert os.path.dirname(handed) == tr._memo_dir()

    # Another process on this node: the same folder, an index of its own, and its first use
    # of the memo is remembering a reply of its own.
    monkeypatch.setattr(tr, '_voiced_memo', OrderedDict())
    monkeypatch.setattr(tr, '_voiced_memo_dir', {})
    second_process = _router(['f5_tts'], _speaks(files))
    _say(second_process, 'Well done.', files)

    assert os.path.isfile(handed), 'still served at /tts/audio for the process that handed it out'
    with open(handed, 'rb') as fh:
        assert fh.read() == b'RIFF-cloned'
    assert len(_memo_files()) == 2


def test_a_first_use_deletes_nothing_the_folder_is_the_pruners(tmp_path):
    folder = tmp_path / 'models' / 'tts_memo' / 'output'
    folder.mkdir(parents=True)
    old, recent = folder / 'old.wav', folder / 'recent.wav'
    for p in (old, recent):
        p.write_bytes(b'RIFF')
    _age(old, 30 * 24 * 3600)
    assert tr._memo_dir() == str(folder)
    assert old.exists() and recent.exists(), 'core.spoken_media prunes this folder, not the memo'


def test_the_memo_folder_follows_the_one_models_root(tmp_path, monkeypatch):
    """rv_tts P2: through model_storage.get_base_dir (it expands '~'), so the folder served and pruned
    is the one the memo writes."""
    import importlib
    # the module, not the package attribute (service_tools/__init__ binds the singleton instance)
    model_storage = importlib.import_module('integrations.service_tools.model_storage')
    monkeypatch.setattr(model_storage, 'get_base_dir', lambda: tmp_path / 'elsewhere')
    assert tr._memo_dir() == str(tmp_path / 'elsewhere' / 'tts_memo' / 'output')


def test_handing_a_file_out_refreshes_its_time_so_the_pruner_takes_it_last(files, workers):
    workers['f5_tts'].alive = True
    router = _router(['f5_tts'], _speaks(files))
    _say(router, 'Great job!', files)
    (own,) = _memo_files()
    _age(own, 3600)
    before = os.path.getmtime(own)
    assert _say(router, 'Great job!', files).path == own
    assert os.path.getmtime(own) > before


def test_a_dropped_entry_is_left_for_the_pruner(files, workers, monkeypatch):
    monkeypatch.setattr(tr, 'VOICED_MEMO_ENTRIES', 1)
    workers['f5_tts'].alive = True
    router = _router(['f5_tts'], _speaks(files))
    _say(router, 'Great job!', files)
    handed = _say(router, 'Great job!', files).path
    _say(router, 'Well done.', files)  # the memo holds one entry: 'Great job!' is dropped
    assert os.path.isfile(handed), 'handed out a moment ago: it may still be playing'


def test_a_file_the_pruner_removed_is_spoken_again(files, workers):
    workers['f5_tts'].alive = True
    router = _router(['f5_tts'], _speaks(files))
    _say(router, 'Great job!', files)
    (own,) = _memo_files()
    os.remove(own)  # core.spoken_media pruned it
    again = _say(router, 'Great job!', files)
    assert 'remembered' not in again.warnings and router._execute.call_count == 2


# ── F2: the throttle limits attempts, never a re-warm of an engine that stopped ──

def test_a_cloner_that_stopped_after_its_warm_up_is_brought_up_again_after_the_floor(files, workers):
    f5 = workers['f5_tts']

    def start(cand, text, lang, voice, out):
        f5.alive = True  # an engine loads on its first call
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    router = _router(['f5_tts'], start)

    assert _say(router, 'Hello there.', files).warming == 'f5_tts'
    _join_warm_ups()
    assert f5.alive and router._execute.call_count == 1

    f5.alive = False  # idled out (ToolWorker idle 300 s) or evicted for another model
    _say(router, 'Good morning.', files)  # within the floor: not reloaded on every reply (R3)
    _join_warm_ups()
    assert router._execute.call_count == 1

    tr._warmed_at['f5_tts'] -= tr.WARM_UP_FLOOR_S + 1  # the floor has passed
    again = _say(router, 'Well done.', files)
    _join_warm_ups()
    assert again.warming == 'f5_tts'
    assert router._execute.call_count == 2, 'brought up again after the floor, not 600 s later'
    assert f5.alive


def test_a_lower_engine_coming_up_never_lifts_the_heads_throttle(files, workers):
    """rv_tts R3 (b): the head failed and the next engine came up; when that one idles out, the head
    is not retried on every reply."""
    cosy = workers['cosyvoice3']

    def ladder(cand, text, lang, voice, out):
        if cand.engine.engine_id == 'f5_tts':
            return {'error': 'CUDA out of memory'}
        cosy.alive = True
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    router = _router(['f5_tts', 'cosyvoice3'], ladder)
    _say(router, 'Hello there.', files)
    _join_warm_ups()
    assert router._execute.call_count == 2 and cosy.alive
    cosy.alive = False  # the lower engine idles out
    for text in ('Good morning.', 'Well done.'):
        _say(router, text, files)
        _join_warm_ups()
    assert router._execute.call_count == 2, 'the failed head waits its WARM_UP_EVERY_S'


def test_a_head_that_never_starts_never_holds_back_the_engine_below_it(files, workers):
    """rv_tts R4: a head this router cannot run (omnivoice, neutts_air: 'Unknown engine') fails at once
    every time; the cloner below it that came up is brought up again after ITS floor once it idles
    out, not 600 s after the attempt."""
    cosy = workers['cosyvoice3']
    tried = []

    def ladder(cand, text, lang, voice, out):
        tried.append(cand.engine.engine_id)
        if cand.engine.engine_id == 'f5_tts':
            return {'error': 'Unknown engine'}
        cosy.alive = True
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    router = _router(['f5_tts', 'cosyvoice3'], ladder)
    _say(router, 'Hello there.', files)
    _join_warm_ups()
    assert tried == ['f5_tts', 'cosyvoice3'] and cosy.alive

    cosy.alive = False  # idled out (300 s)
    tr._warmed_at['cosyvoice3'] -= tr.WARM_UP_FLOOR_S + 1  # its floor has passed; f5's 600 s has not
    again = _say(router, 'Good morning.', files)
    _join_warm_ups()
    assert again.warming == 'cosyvoice3'
    assert tried == ['f5_tts', 'cosyvoice3', 'cosyvoice3'], 'the throttled head is skipped, not retried'
    assert cosy.alive


def test_a_cloner_that_failed_to_start_is_not_retried_on_every_reply(files, workers):
    router = _router(['f5_tts'], lambda *a: {'error': 'CUDA out of memory'})
    for text in ('Hello there.', 'Good morning.', 'Well done.'):
        assert _say(router, text, files).warming == 'f5_tts'
        _join_warm_ups()
    assert router._execute.call_count == 1, 'one attempt per WARM_UP_EVERY_S'


def test_a_warm_up_that_answered_but_left_nothing_running_is_still_throttled(files, workers):
    """An answer that left no engine running keeps the full stamp: were every answer to lift the
    throttle, each realtime reply would start another warm-up."""
    router = _router(['f5_tts'], _speaks(files))  # answers; f5's worker never comes up
    for text in ('Hello there.', 'Good morning.', 'Well done.'):
        assert _say(router, text, files).warming == 'f5_tts'
        _join_warm_ups()
    assert router._execute.call_count == 1
    assert not workers['f5_tts'].alive


def test_a_second_reply_while_a_warm_up_is_in_flight_starts_no_other(files, workers):
    started, release = threading.Event(), threading.Event()
    f5 = workers['f5_tts']

    def slow_start(cand, text, lang, voice, out):
        started.set()
        release.wait(5)
        f5.alive = True
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    router = _router(['f5_tts'], slow_start)

    _say(router, 'Hello there.', files)
    assert started.wait(5)
    assert _say(router, 'Good morning.', files).warming == 'f5_tts'
    release.set()
    _join_warm_ups()
    assert router._execute.call_count == 1, 'one warm-up at a time'


def test_a_different_ladder_while_a_warm_up_is_in_flight_starts_no_other(files, workers):
    """rv_tts TG1: the lock itself, not an engine's own stamp, keeps warm-ups one at a time; a reply in
    another language (another ladder, nothing throttled) is told one is running, truthfully."""
    started, release = threading.Event(), threading.Event()
    f5 = workers['f5_tts']

    def slow_start(cand, text, lang, voice, out):
        started.set()
        release.wait(5)
        f5.alive = True
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    first = _router(['f5_tts'], slow_start)
    _say(first, 'Hello there.', files)
    assert started.wait(5)
    second = _router(['cosyvoice3'], lambda *a: pytest.fail('a second warm-up started'))
    other = _say(second, 'Namaste.', files)
    release.set()
    _join_warm_ups()
    assert second._execute.call_count == 0
    assert 'another warm-up is running' in other.error and other.warming == 'cosyvoice3'

