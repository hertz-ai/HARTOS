"""The lifecycle's disk phase keeps the replies' stored voices and faces within their share of the disk
(review hartos-17 of the stored face, MUST 3: phase 6 CALLS core.spoken_media.prune_spoken_media; the
lifecycle names no folder).  Before this call the pruner existed and nothing ran it, so stored faces
(about 0.3 MB a second of reply) would have grown without a cap.
"""
import importlib

import pytest

lifecycle = importlib.import_module('integrations.service_tools.model_lifecycle')
spoken_media = importlib.import_module('core.spoken_media')


@pytest.fixture
def mgr(monkeypatch):
    import types
    m = lifecycle.ModelLifecycleManager()
    clock = {'now': 1000.0}
    # the module's own clock, stubbed the way test_model_lifecycle stubs it
    monkeypatch.setattr(lifecycle, 'time', types.SimpleNamespace(time=lambda: clock['now']))
    m.clock = clock
    return m


@pytest.fixture
def prunes(monkeypatch):
    calls = []
    monkeypatch.setattr(spoken_media, 'prune_spoken_media', lambda **kw: calls.append(kw) or {})
    return calls


def test_the_first_tick_prunes_then_not_again_within_ten_minutes(mgr, prunes):
    assert mgr._maybe_prune_spoken_media(False) is True
    mgr.clock['now'] += mgr.SPOKEN_MEDIA_PRUNE_S - 1
    assert mgr._maybe_prune_spoken_media(False) is False
    mgr.clock['now'] += 1
    assert mgr._maybe_prune_spoken_media(False) is True
    assert len(prunes) == 2


def test_a_short_disk_prunes_every_minute(mgr, prunes):
    mgr._maybe_prune_spoken_media(True)
    mgr.clock['now'] += mgr.SPOKEN_MEDIA_PRUNE_UNDER_PRESSURE_S - 1
    assert mgr._maybe_prune_spoken_media(True) is False
    mgr.clock['now'] += 1
    assert mgr._maybe_prune_spoken_media(True) is True
    assert len(prunes) == 2


def test_a_prune_that_fails_never_breaks_the_tick(mgr, monkeypatch, caplog):
    def boom(**kw):
        raise OSError('disk gone')
    monkeypatch.setattr(spoken_media, 'prune_spoken_media', boom)
    assert mgr._maybe_prune_spoken_media(False) is True  # logged, not raised
    assert 'spoken media: prune failed' in caplog.text


def test_phase_six_of_the_tick_calls_it_with_the_disk_reading(mgr, monkeypatch):
    """The tick's other phases are stubbed: only phase 6's wiring is under test."""
    for name in ('_refresh_memory_state', '_wd_heartbeat', '_update_priorities',
                 '_respond_to_vram_pressure', '_respond_to_ram_pressure', '_respond_to_cpu_pressure',
                 '_evict_idle_models', '_apply_hive_hints', '_report_to_federation',
                 '_check_process_health', '_wait_for_due_llm_restart', '_process_restart_queue',
                 '_process_swap_queue', '_emit_pressure_alerts', '_persist_state_to_disk'):
        monkeypatch.setattr(mgr, name, lambda *a, **k: None)
    for name in ('_detect_vram_pressure', '_detect_ram_pressure', '_detect_cpu_pressure'):
        monkeypatch.setattr(mgr, name, lambda: False)
    monkeypatch.setattr(mgr, '_detect_disk_pressure', lambda: True)
    seen = []
    monkeypatch.setattr(mgr, '_maybe_prune_spoken_media', lambda pressure: seen.append(pressure))
    mgr._running = False
    mgr._tick()
    assert seen == [True]
