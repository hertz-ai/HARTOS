"""TTSRouter.synthesize, the one synthesis entry: what a caller someone is waiting on gets.

Owner 2026-11-26: "while setting up a cloning voice TTS we can continue to serve with whichever
TTS engine currently serves, also for realtime conversations"; and "the shared router is the
canonical place".  Pinned here, in the router:

  * dispatch is exactly as before this change (build #6 split, coordinator 11-26): engines
    with a row and no branch (omnivoice, neutts_air) are still not run, so a Tamil call
    still lands where it did before (mms_tts here, measured at HEAD); the registry dispatch comes after the demo with a live check;
  * live state (engine_running) comes from the engine's registry row: the ToolWorker is alive, or an
    in-process engine's is_loaded();
  * running_only: only engines already running speak -- a cold, preferred engine never starts
    on the caller's path (review rv_9a3a P8); with none running, the best one is brought up off
    the path, once, and the answer names it (warming);
  * a short reply in a recorded voice is remembered and heard again at once, with no engine,
    also while none is running -- never across a re-recorded voice, never a long reply, never
    the default voice.
"""
import os
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.unit.module_swap import swap_modules  # noqa: E402

from integrations.channels.media import tts_router as tr  # noqa: E402
from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402

CLONERS = ('chatterbox_turbo', 'f5_tts', 'neutts_air', 'omnivoice')


class _Worker:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def stop(self):
        pass


def _running(*alive, pocket=False):
    """Fake tool modules for every engine row: only ``alive`` engines have a live worker."""
    mods = {}
    for eid, seed in tr._SEED_SPECS.items():
        if not seed.tool_module or not seed.tool_worker_attr:
            continue
        mod = mods.setdefault(seed.tool_module, types.ModuleType(seed.tool_module))
        setattr(mod, seed.tool_worker_attr, _Worker(eid in alive))
    pocket_mod = types.ModuleType('integrations.service_tools.pocket_tts_tool')
    pocket_mod.is_loaded = lambda: pocket
    mods['integrations.service_tools.pocket_tts_tool'] = pocket_mod
    return swap_modules(mods)


def _cand(eid):
    spec = tr.ENGINE_REGISTRY[eid]
    return tr.TTSCandidate(engine=spec, location=tr.TTSLocation.LOCAL, device='gpu',
                           estimated_latency_ms=100, quality_score=spec.quality)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    tr._voiced_memo.clear()
    tr._warmed_at.clear()
    tr._voiced_memo_dir.clear()
    monkeypatch.setenv('HEVOLVE_MODEL_DIR', str(tmp_path / 'models'))
    monkeypatch.setattr(tn, 'normalize_for_tts', lambda t, *a, **k: t)
    audits = []
    monkeypatch.setattr(tr, '_audit_speech', lambda text, result, cand, *a: audits.append(cand.engine.engine_id))
    yield audits
    for t in threading.enumerate():
        if t.name == 'tts-warm-up':
            t.join(5)
    tr._voiced_memo.clear()
    tr._warmed_at.clear()


@pytest.fixture
def files(tmp_path):
    voice = tmp_path / 'voice.wav'
    voice.write_bytes(b'RIFF-recording-1')
    out = tmp_path / 'cloned.wav'
    out.write_bytes(b'RIFF')
    return types.SimpleNamespace(voice=str(voice), out=str(out))


def _router(ladder, files):
    router = tr.TTSRouter()
    router.select_engines = MagicMock(return_value=[_cand(e) for e in ladder])
    router._execute = MagicMock(side_effect=lambda cand, text, lang, voice, out: {
        'path': files.out, 'duration': 1.0, 'sample_rate': 24000, 'voice': voice})
    return router


def _engines_run(router):
    return [c.args[0].engine.engine_id for c in router._execute.call_args_list]


# ── dispatch: exactly as before this change ───────────────────────────────

AS_BEFORE = {
    'cosyvoice3': ('integrations.service_tools.cosyvoice_tool', 'cosyvoice_synthesize'),
    'indic_parler': ('integrations.service_tools.indic_parler_tool', 'indic_parler_synthesize'),
    'chatterbox_turbo': ('integrations.service_tools.chatterbox_tool', 'chatterbox_synthesize'),
    'chatterbox_ml': ('integrations.service_tools.chatterbox_tool', 'chatterbox_ml_synthesize'),
    'f5_tts': ('integrations.service_tools.f5_tts_tool', 'f5_synthesize'),
    'kokoro': ('integrations.service_tools.kokoro_tool', 'kokoro_synthesize'),
    'melotts': ('integrations.service_tools.melotts_tool', 'melotts_synthesize'),
    'xtts_v2': ('integrations.service_tools.xtts_tool', 'xtts_synthesize'),
    'mms_tts': ('integrations.service_tools.mms_tts_tool', 'mms_tts_synthesize'),
}


@pytest.mark.parametrize('eid', sorted(AS_BEFORE))
def test_an_engine_runs_where_it_ran_before(eid):
    router = tr.TTSRouter()
    router._call_gpu_engine = MagicMock(return_value={'path': 'x.wav'})
    router._execute(_cand(eid), 'hi', 'en', None, None)
    assert router._call_gpu_engine.call_args.args[:2] == AS_BEFORE[eid]


@pytest.mark.parametrize('eid', ['omnivoice', 'neutts_air'])
def test_no_engine_becomes_reachable_in_this_build(eid):
    """omnivoice and neutts_air have rows and no branch: still not run (the registry dispatch
    lands after the demo, with a live Tamil check and a GPU memory reading)."""
    router = tr.TTSRouter()
    router._call_gpu_engine = MagicMock(side_effect=AssertionError('started'))
    assert 'Unknown engine' in router._execute(_cand(eid), 'hi', 'en', None, None)['error']


def test_a_tamil_call_lands_where_it_did_before_never_on_omnivoice(monkeypatch):
    """omnivoice is on the Tamil ladder and is not run in this build, so the call lands on
    the engine it landed on before this change.  Measured with HEAD's router (44de14935) under
    this exact setup -- every engine installed, the GPU fits, espeak's binary absent: order
    espeak, omnivoice, mms_tts, chatterbox_ml, indic_parler; HEAD spoke with mms_tts."""
    monkeypatch.setattr(tr, '_is_engine_installed', lambda eid: True)
    monkeypatch.setattr(tr, '_get_gpu_info', lambda: {'cuda_available': True})
    monkeypatch.setattr(tr, '_can_fit_on_gpu', lambda eid: True)
    monkeypatch.setattr(tr, '_get_compute_policy', lambda: {'compute_policy': 'local_only'})
    router = tr.TTSRouter()
    router._call_espeak = lambda *a, **k: {'error': 'espeak is not on this machine'}
    calls = []

    def call(module, fn, text, language, voice, output_path):
        calls.append(module)
        return {'path': 'ta.wav', 'duration': 1.0}
    router._call_gpu_engine = call
    result = router.synthesize('வணக்கம்', language='ta')
    assert result.engine_id == 'mms_tts'
    assert calls == ['integrations.service_tools.mms_tts_tool']


def test_the_warm_up_walks_the_ladder_past_an_engine_it_cannot_run(files):
    """A voiced Tamil call: omnivoice heads the cloners and cannot be run here, so the
    warm-up goes on to the next cloner rather than retrying omnivoice forever."""
    router = _router(['omnivoice', 'chatterbox_ml'], files)
    router._execute.side_effect = lambda cand, text, lang, voice, out: (
        {'error': f'Unknown engine: {cand.engine.engine_id}'} if cand.engine.engine_id == 'omnivoice'
        else {'path': files.out, 'duration': 1.0, 'voice': voice})
    with _running():
        result = router.synthesize('வணக்கம்', language='ta', voice=files.voice, running_only=True)
        for t in threading.enumerate():
            if t.name == 'tts-warm-up':
                t.join(5)
    assert result.warming == 'omnivoice'
    assert _engines_run(router) == ['omnivoice', 'chatterbox_ml']


def test_a_running_engine_the_router_cannot_run_does_not_block_the_warm_up(files):
    """rv_b6: neutts_air's worker is up (another caller started it) but this router answers it
    'Unknown engine'.  It must not count as "a cloner is running": the realtime call answers
    warming for the runnable cloner and the warm-up brings that one up."""
    router = _router(['neutts_air', 'f5_tts'], files)
    router._execute.side_effect = lambda cand, text, lang, voice, out: (
        {'error': 'Unknown engine: neutts_air'} if cand.engine.engine_id == 'neutts_air'
        else {'path': files.out, 'duration': 1.0, 'voice': voice})
    with _running('neutts_air'):
        result = router.synthesize('Great job!', language='en', voice=files.voice, running_only=True)
        for t in threading.enumerate():
            if t.name == 'tts-warm-up':
                t.join(5)
    assert result.warming == 'f5_tts' and not result.path
    assert _engines_run(router) == ['neutts_air', 'f5_tts'], 'tried, failed at once; f5 warmed'


# ── live state, from the row ──────────────────────────────────────────────────

def test_running_is_the_engines_worker_alive():
    with _running('f5_tts'):
        assert tr.engine_running(tr.ENGINE_REGISTRY['f5_tts'])
        assert not tr.engine_running(tr.ENGINE_REGISTRY['chatterbox_turbo'])


def test_an_engine_whose_module_was_never_imported_is_not_running():
    with swap_modules({tr._SEED_SPECS['f5_tts'].tool_module: None}):
        assert not tr.engine_running(tr.ENGINE_REGISTRY['f5_tts'])


def test_an_in_process_engine_answers_through_is_loaded():
    with _running(pocket=True):
        assert tr.engine_running(tr.ENGINE_REGISTRY['pocket_tts'])
    with _running(pocket=False):
        assert not tr.engine_running(tr.ENGINE_REGISTRY['pocket_tts'])


# ── running_only: a caller someone is waiting on ─────────────────────────────

def test_a_running_cloner_speaks_never_a_cold_preferred_one(files):
    """rv_9a3a P8: chatterbox_turbo installed, cold and first on the ladder; f5_tts running."""
    router = _router(['chatterbox_turbo', 'f5_tts', 'espeak'], files)
    with _running('f5_tts'):
        result = router.synthesize('Hello there.', language='en', voice=files.voice,
                                   running_only=True)
    assert result.engine_id == 'f5_tts' and result.path == files.out
    assert _engines_run(router) == ['f5_tts']


def test_with_none_running_the_best_is_brought_up_off_the_callers_path(files):
    router = _router(['chatterbox_turbo', 'f5_tts', 'espeak'], files)
    started = threading.Event()
    release = threading.Event()

    def slow_start(cand, text, lang, voice, out):
        started.set()
        release.wait(5)
        return {'path': files.out, 'duration': 1.0, 'voice': voice}
    router._execute.side_effect = slow_start
    with _running():
        result = router.synthesize('Hello there.', language='en', voice=files.voice,
                                   running_only=True)
        assert result.error and result.warming == 'chatterbox_turbo'  # answered at once
        assert started.wait(5), 'the warm-up runs on its own thread'
        again = router.synthesize('Hello there.', language='en', voice=files.voice,
                                  running_only=True)
        assert again.warming == 'chatterbox_turbo'
        release.set()
    for t in threading.enumerate():
        if t.name == 'tts-warm-up':
            t.join(5)
    assert _engines_run(router) == ['chatterbox_turbo'], 'one warm-up, not one per call'


def test_an_engine_that_cannot_clone_never_answers_a_voiced_realtime_call(files):
    """espeak is appended to every ladder and ignores the voice: running or not, it is no
    cloner, so a voiced call with no cloner running warms one instead."""
    router = _router(['f5_tts', 'espeak'], files)
    with _running(pocket=True):  # espeak's row names the pocket module
        result = router.synthesize('Hello there.', language='en', voice=files.voice,
                                   running_only=True)
    assert result.warming == 'f5_tts'


def test_nothing_installed_is_said_as_before(files):
    router = _router([], files)
    result = router.synthesize('Hello there.', language='en', voice=files.voice, running_only=True)
    assert result.error == 'No TTS engine is installed for this language' and not result.warming


def test_a_caller_that_can_wait_brings_the_preferred_one_up_on_demand(files):
    router = _router(['chatterbox_turbo', 'f5_tts'], files)
    with _running('f5_tts'):
        result = router.synthesize('Hello there.', language='en', voice=files.voice)
    assert result.engine_id == 'chatterbox_turbo'


# ── memoized: a short voiced reply is heard again at once ────────────────────

def test_heard_again_with_no_engine_even_while_none_runs(files, fresh):
    router = _router(['f5_tts'], files)
    with _running('f5_tts'):
        first = router.synthesize('Great job!', language='en', voice=files.voice, running_only=True)
    with _running():
        again = router.synthesize('Great job!', language='en', voice=files.voice, running_only=True)
    assert 'remembered' in again.warnings and again.engine_id == 'f5_tts'
    assert again.path != first.path, 'each call gets a file of its own'
    assert open(again.path, 'rb').read() == open(first.path, 'rb').read()
    assert _engines_run(router) == ['f5_tts']
    assert fresh == ['f5_tts', 'f5_tts'], 'a remembered reply is still spoken: one audit line each'


def test_a_re_recorded_voice_is_never_answered_with_the_old_one(files):
    router = _router(['f5_tts'], files)
    with _running('f5_tts'):
        router.synthesize('Great job!', language='en', voice=files.voice)
        with open(files.voice, 'wb') as fh:
            fh.write(b'RIFF-recording-2, longer')
        router.synthesize('Great job!', language='en', voice=files.voice)
    assert _engines_run(router) == ['f5_tts', 'f5_tts']


def test_a_long_reply_or_the_default_voice_is_never_remembered(files):
    router = _router(['f5_tts'], files)
    long_text = 'word ' * (tr.VOICED_MEMO_MAX_CHARS // 4)
    with _running('f5_tts'):
        router.synthesize(long_text, language='en', voice=files.voice)
        router.synthesize('Great job!', language='en')
    assert tr._voiced_memo == {}


def test_a_warm_up_remembers_what_it_said(files):
    """The words a warm-up spoke in the voice are the call that will likely repeat."""
    router = _router(['f5_tts'], files)
    with _running():
        router.synthesize('Great job!', language='en', voice=files.voice, running_only=True)
        for t in threading.enumerate():
            if t.name == 'tts-warm-up':
                t.join(5)
        again = router.synthesize('Great job!', language='en', voice=files.voice, running_only=True)
    assert 'remembered' in again.warnings and os.path.isfile(again.path)
    assert _engines_run(router) == ['f5_tts'], 'the warm-up spoke; the repeat did not'


def test_a_caller_overwriting_its_file_never_changes_what_is_remembered(files):
    router = _router(['f5_tts'], files)
    with _running('f5_tts'):
        router.synthesize('Great job!', language='en', voice=files.voice)
        with open(files.out, 'wb') as fh:
            fh.write(b'RIFF-some-other-words')  # the caller reuses its output file
        again = router.synthesize('Great job!', language='en', voice=files.voice)
    assert open(again.path, 'rb').read() == b'RIFF'


def test_a_remembered_reply_is_written_where_the_caller_asked(files, tmp_path):
    router = _router(['f5_tts'], files)
    wanted = str(tmp_path / 'wanted.wav')
    with _running('f5_tts'):
        router.synthesize('Great job!', language='en', voice=files.voice)
        again = router.synthesize('Great job!', language='en', voice=files.voice,
                                  output_path=wanted)
    assert again.path == wanted and open(wanted, 'rb').read() == b'RIFF'


def test_a_call_that_names_its_engine_gets_that_engine(files):
    router = _router(['f5_tts'], files)
    with _running('f5_tts', 'chatterbox_turbo'):
        router.synthesize('Great job!', language='en', voice=files.voice)
        named = router.synthesize('Great job!', language='en', voice=files.voice,
                                  engine_override='chatterbox_turbo')
    assert named.engine_id == 'chatterbox_turbo' and 'remembered' not in named.warnings
