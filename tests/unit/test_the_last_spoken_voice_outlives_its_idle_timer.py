"""The voice last spoken stays loaded through idle time while it is the
active voice; memory pressure, an LRU eviction or an explicit stop still
take it down.

WHAT WENT WRONG (live walk, 23 Nov 2026, installed Nunba, local 4B)
  Nunba pins the active voice (J214, set_pressure_evict_only), and the pin
  never held for a voice served by a worker:
  * Nunba pinned 'tts_mms_tts' (the VRAM budget name, "tracked=False"); the
    lifecycle tracks the voice as 'tts-mms-tts' (its catalog id).
  * Each synth re-registers the voice (ModelOrchestrator._register_lifecycle)
    with a fresh state at default_idle_evict, wiping any pin; the idle sweep
    evicted 'tts-mms-tts' at 17:03:53 (idle 329 s > 300 s).
  * The worker's own idle timer (ToolWorker._on_idle, 300 s) asks nobody;
    it stopped the mms_tts worker at 17:03:27.
  So a voice warmed at boot was cold again 5 minutes later.

THE RULE
  * One name: the catalog id, tts_router._engine_id_to_catalog_id; the
    worker looks its pin up by it (ToolWorker._lifecycle_name).
  * A re-register keeps the pin the model holds, and takes one staged
    before the model was tracked.
  * The worker's idle timer keeps a worker whose model the lifecycle
    holds (ModelLifecycleManager.holds: pinned, or pressure_evict_only with
    no pressure on the memory it sits in) and looks again an idle period
    later.  Memory pressure, the lifecycle's pressure phase, LRU eviction
    (try_free_vram) and an explicit stop() still end it; so does moving the
    pin to another voice.

Real ModelLifecycleManager, ModelOrchestrator._register_lifecycle and
ToolWorker; stand-ins only for the worker process, the pressure readings
and the VRAM reading.

    python -m pytest tests/unit/test_the_last_spoken_voice_outlives_its_idle_timer.py -q --noconftest
"""
import threading
import time
import types

import pytest

VOICE = 'tts-mms-tts'
IDLE = 300.0


class _Process:
    """The worker subprocess: alive until stopped."""

    def __init__(self):
        self.alive = True
        self.stops = 0

    def is_alive(self):
        return self.alive

    def stop(self, timeout=5.0):
        self.stops += 1
        self.alive = False


@pytest.fixture
def mlm(monkeypatch):
    """A fresh lifecycle manager as the process's one, empty, with the
    pressure readings set per test."""
    from integrations.service_tools import model_lifecycle as ml
    mgr = ml.ModelLifecycleManager()
    mgr._models.clear()
    mgr._persisted_hints.clear()
    pressure = {'vram': False, 'ram': False}
    monkeypatch.setattr(mgr, '_detect_vram_pressure', lambda: pressure['vram'])
    monkeypatch.setattr(mgr, '_detect_ram_pressure', lambda: pressure['ram'])
    unloaded = []
    monkeypatch.setattr(mgr, '_do_unload', unloaded.append)
    monkeypatch.setattr(ml, '_lifecycle_manager', mgr)
    return types.SimpleNamespace(mgr=mgr, pressure=pressure, unloaded=unloaded, ml=ml)


@pytest.fixture
def worker(monkeypatch):
    """The mms_tts ToolWorker with a live (stand-in) process, idle for
    longer than its idle timeout."""
    from integrations.service_tools.gpu_worker import ToolWorker
    made = []

    def make(tool_name='mms_tts'):
        tw = ToolWorker(tool_name=tool_name,
                        tool_module='integrations.service_tools.mms_tts_tool',
                        vram_budget='tts_mms_tts', output_subdir='pin_test',
                        engine='mms-tts', idle_timeout=IDLE)
        tw._release_vram = lambda: None
        tw._worker = _Process()
        tw._last_used = time.monotonic() - (IDLE + 1)
        made.append(tw)
        return tw

    yield make
    for tw in made:
        if tw._idle_timer is not None:
            tw._idle_timer.cancel()


def _register(device='cpu'):
    """What each synth of the voice does: the orchestrator registers it."""
    from integrations.service_tools.model_catalog import ModelEntry
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    # Its row's own idle time, as tts_router.populate_tts_catalog writes it:
    # the record takes its row's timeout (review of 5ecf725e8).
    entry = ModelEntry(id=VOICE, name='MMS-TTS', model_type='tts', idle_timeout_s=300.0)
    entry.device = device
    ModelOrchestrator()._register_lifecycle(entry)


def _idle_sweep(mgr, idle_s=400.0):
    """Phases 2 and 7 of the lifecycle tick, the model idle for idle_s."""
    with mgr._lock:
        mgr._models[VOICE].last_access_time = time.time() - idle_s
    mgr._update_priorities()
    mgr._evict_idle_models()


def test_one_name_for_the_voice_on_every_side(worker):
    from integrations.channels.media.tts_router import _engine_id_to_catalog_id
    assert _engine_id_to_catalog_id('mms_tts') == VOICE
    assert worker()._lifecycle_name() == VOICE
    # Not a voice: its own name, as the orchestrator tracks whisper.
    assert worker('whisper')._lifecycle_name() == 'whisper'


def test_the_active_voice_outlives_more_than_300_s_idle(mlm, worker):
    _register()
    mlm.mgr.set_pressure_evict_only(VOICE, True)      # Nunba, J214
    _register()                                         # the next synth
    with mlm.mgr._lock:
        assert mlm.mgr._models[VOICE].pressure_evict_only is True, (
            'a re-register wiped the pin')
    _idle_sweep(mlm.mgr)
    assert mlm.unloaded == [], 'the idle sweep evicted the pinned voice'
    tw = worker()
    tw._on_idle()
    assert tw._worker is not None and tw._worker.stops == 0, (
        'the worker idle timer stopped the pinned voice after 301 s')
    assert tw._idle_timer is not None and tw._idle_timer.is_alive(), (
        'the held worker is not looked at again')


def test_a_pin_staged_before_the_voice_is_tracked_holds_it(mlm, worker):
    """The boot warm-up pins the voice before any synth registers it."""
    r = mlm.mgr.set_pressure_evict_only(VOICE, True)
    assert r['tracked'] is False
    tw = worker()
    tw._on_idle()
    assert tw._worker is not None and tw._worker.stops == 0
    _register()
    with mlm.mgr._lock:
        assert mlm.mgr._models[VOICE].pressure_evict_only is True, (
            'the staged pin was not taken when the voice was registered')
        assert 'pressure_evict_only' not in mlm.mgr._persisted_hints.get(VOICE, {})


def test_an_unpinned_voice_still_stops_after_its_idle_time(mlm, worker):
    _register()
    tw = worker()
    tw._on_idle()
    assert tw._worker is None and tw._idle_timer is None


def test_a_moved_pin_lets_the_old_voice_go(mlm, worker):
    """Pins are bounded: set_language unpins the voice it leaves."""
    _register()
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    mlm.mgr.set_pressure_evict_only(VOICE, False)
    _register()
    _idle_sweep(mlm.mgr)
    assert mlm.unloaded == [VOICE]
    tw = worker()
    tw._on_idle()
    assert tw._worker is None


@pytest.mark.parametrize('device, pressure, stops', [
    ('cpu', 'ram', True),       # a CPU voice yields to RAM pressure
    ('gpu', 'vram', True),      # a GPU voice yields to VRAM pressure
    ('cpu', 'vram', False),     # stopping a CPU voice frees no VRAM
    ('gpu', 'ram', False),      # nor a GPU voice RAM (phase 4 takes CPU models)
])
def test_memory_pressure_still_stops_a_pinned_voice(mlm, worker, device, pressure, stops):
    _register(device)
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    mlm.pressure[pressure] = True
    tw = worker()
    tw._on_idle()
    assert (tw._worker is None) is stops


def test_a_staged_pin_yields_to_either_pressure(mlm, worker):
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    mlm.pressure['vram'] = True
    tw = worker()
    tw._on_idle()
    assert tw._worker is None


def test_the_lifecycle_pressure_phase_still_takes_a_pinned_gpu_voice(mlm):
    _register('gpu')
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    with mlm.mgr._lock:
        mlm.mgr._models[VOICE].last_access_time = time.time() - 400
    mlm.mgr._update_priorities()
    mlm.pressure['vram'] = True
    mlm.mgr._respond_to_vram_pressure()
    assert mlm.unloaded == [VOICE]


def test_lru_eviction_still_stops_a_pinned_voice(mlm, worker, monkeypatch):
    import importlib
    from integrations.service_tools import gpu_worker
    # The module, not the package's same-named VRAMManager attribute.
    vm = importlib.import_module('integrations.service_tools.vram_manager')
    _register('gpu')
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    tw = worker()
    free = {'gb': 0.5}

    def _stop():
        tw._worker.stop()
        tw._worker = None
        free['gb'] = 3.0
    tw.stop = _stop
    monkeypatch.setattr(vm.vram_manager, 'get_free_vram', lambda: free['gb'])
    assert gpu_worker.try_free_vram(2.0, exclude_tool='indic_parler') is True
    assert tw._worker is None


def test_an_explicit_stop_still_stops_a_pinned_voice(mlm, worker):
    _register()
    mlm.mgr.set_pressure_evict_only(VOICE, True)
    tw = worker()
    proc = tw._worker
    tw.stop()
    assert tw._worker is None and proc.stops == 1
