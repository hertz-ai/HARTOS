"""A swap frees the GPU by unloading the idle model that has waited longest,
and a model whose kind the lifecycle cannot tell is named, never a reason to
stop tracking the others.

Review of 6717104f6 (rv_mt, 2026-11-24).  ``request_swap`` sorted its
candidates by (priority rank, last access) ascending and took the last one,
so inside the most evictable class it unloaded the model used MOST recently:
on a tight-GPU voice turn, the voice or speech model the person had just used,
not the one idle longest.  Before 6717104f6 the same scan raised
AttributeError and unloaded nothing, so this order had never run.

The same review found three smaller things fixed here:
- a runtime tool with no catalog row and no declared model_type raised out of
  ``_on_tool_started``: the runtime manager logged it at debug level only, and
  ``_sync_from_rtm`` stopped at that tool, so the live tools after it were
  never recorded;
- ``_handle_dead_process`` told the catalog the dead tool's kind by guessing
  from its name ('wan2gp' -> 'wan2gp'), although the record carries the kind;
- ``_get_model_capabilities`` called ``catalog.list_ids()``, which does not
  exist, and logged a traceback on every tool start.
"""
import logging
import time
import types
from unittest.mock import MagicMock

from integrations.service_tools import model_lifecycle as ml
from integrations.service_tools.model_lifecycle import (
    ModelDevice, ModelLifecycleManager, ModelPriority, ModelState,
)


def _mgr():
    mlm = ModelLifecycleManager()
    mlm._do_unload = MagicMock()
    mlm._emit_event = MagicMock()
    return mlm


def _gpu(mlm, name, kind, priority, idle_s, busy=0):
    mlm._models[name] = ModelState(
        name=name, model_type=kind, device=ModelDevice.GPU,
        priority=priority, last_access_time=time.time() - idle_s,
        active_inference_count=busy)


# ── which model a swap unloads ─────────────────────────────────────────

def test_a_swap_unloads_the_model_idle_longest_not_the_one_just_used():
    mlm = _mgr()
    _gpu(mlm, 'voice_just_used', 'tts', ModelPriority.EVICTABLE, 5)
    _gpu(mlm, 'speech_a_while_ago', 'stt', ModelPriority.EVICTABLE, 200)
    _gpu(mlm, 'vision_long_ago', 'vlm', ModelPriority.EVICTABLE, 900)

    assert mlm.request_swap('caption') is True
    mlm._do_unload.assert_called_once_with('vision_long_ago')
    assert mlm._swap_queue[-1]['name'] == 'vision_long_ago'


def test_the_most_evictable_class_goes_first_even_when_used_more_recently():
    mlm = _mgr()
    _gpu(mlm, 'warm_but_old', 'tts', ModelPriority.WARM, 900)
    _gpu(mlm, 'idle_older', 'stt', ModelPriority.IDLE, 600)
    _gpu(mlm, 'evictable_recent', 'vlm', ModelPriority.EVICTABLE, 10)

    assert mlm.request_swap('caption') is True
    mlm._do_unload.assert_called_once_with('evictable_recent')


def test_a_swap_never_unloads_a_language_model_the_requester_or_a_busy_one():
    mlm = _mgr()
    _gpu(mlm, 'llm', 'llm', ModelPriority.EVICTABLE, 5000)
    _gpu(mlm, 'llm-draft', 'llm', ModelPriority.EVICTABLE, 5000)
    _gpu(mlm, 'main_chat', 'llm', ModelPriority.EVICTABLE, 5000)  # typed, not named llm-
    _gpu(mlm, 'caption', 'vlm', ModelPriority.EVICTABLE, 5000)   # the requester itself
    _gpu(mlm, 'active_voice', 'tts', ModelPriority.ACTIVE, 5000)
    _gpu(mlm, 'speaking_voice', 'tts', ModelPriority.EVICTABLE, 5000, busy=1)
    _gpu(mlm, 'speech', 'stt', ModelPriority.EVICTABLE, 30)

    assert mlm.request_swap('caption') is True
    mlm._do_unload.assert_called_once_with('speech')


# ── a tool whose kind the lifecycle cannot tell ────────────────────────

def test_a_tool_with_no_kind_is_named_in_a_warning_and_left_untracked(
        monkeypatch, caplog):
    from integrations.service_tools import runtime_manager as rtm
    monkeypatch.setitem(rtm.TOOL_CONFIGS, 'untyped_tool', {'catalog_id': None})
    mlm = _mgr()

    with caplog.at_level(logging.WARNING, logger=ml.logger.name):
        mlm._on_tool_started('untyped_tool', device='gpu')   # does not raise

    assert 'untyped_tool' not in mlm._models
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any('untyped_tool' in m and 'model_type' in m and 'TOOL_CONFIGS' in m
               for m in warnings), warnings


def test_a_tool_with_no_kind_does_not_stop_the_startup_sync(monkeypatch):
    from integrations.service_tools import runtime_manager as rtm
    configs = {'untyped_tool': {'catalog_id': None},
               'whisper': rtm.TOOL_CONFIGS['whisper']}
    monkeypatch.setattr(rtm, 'TOOL_CONFIGS', configs)
    monkeypatch.setattr(rtm.runtime_tool_manager, '_is_server_alive',
                        lambda name: True)
    mlm = _mgr()

    mlm._sync_from_rtm()

    assert 'untyped_tool' not in mlm._models
    assert mlm._models['whisper'].model_type == 'stt'


# ── a dead tool and the catalog ────────────────────────────────────────

def test_a_dead_tool_is_reported_to_the_catalog_as_its_recorded_kind(monkeypatch):
    from integrations.service_tools import model_orchestrator as mo
    orch = MagicMock()
    monkeypatch.setattr(mo, 'get_orchestrator', lambda: orch)
    mlm = _mgr()
    _gpu(mlm, 'wan2gp', 'video_gen', ModelPriority.WARM, 10)

    mlm._handle_dead_process('wan2gp', exit_code=1, proc_type='sidecar')

    orch.notify_unloaded.assert_called_once_with('video_gen', 'wan2gp')


# ── capabilities announced when a tool starts ──────────────────────────

def test_a_tool_finds_its_catalog_capabilities_by_partial_id_without_an_error(
        monkeypatch, caplog):
    from integrations.service_tools import model_catalog as mc
    entry = types.SimpleNamespace(id='vlm-minicpm-v2',
                                  capabilities={'vision': True})
    catalog = types.SimpleNamespace(
        get=lambda model_id: entry if model_id == entry.id else None,
        list_all=lambda: [entry])
    monkeypatch.setattr(mc, 'get_catalog', lambda: catalog)
    mlm = _mgr()

    with caplog.at_level(logging.ERROR, logger=ml.logger.name):
        caps = mlm._get_model_capabilities('minicpm')

    assert caps == {'vision': True}
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


# ── the same order under GPU pressure, and pinned models (rv_b1b9) ─────

def test_gpu_pressure_frees_the_model_idle_longest_first(monkeypatch):
    """_respond_to_vram_pressure walked its candidates newest-first inside
    a class: the same defect as the swap, reached on every tick at 85% VRAM.
    """
    mlm = _mgr()
    _gpu(mlm, 'voice_just_used', 'tts', ModelPriority.EVICTABLE, 5)
    _gpu(mlm, 'vision_long_ago', 'vlm', ModelPriority.EVICTABLE, 900)
    pressure = iter([True, False])           # one unload relieves it
    monkeypatch.setattr(mlm, '_detect_vram_pressure', lambda: next(pressure))

    mlm._respond_to_vram_pressure()

    mlm._do_unload.assert_called_once_with('vision_long_ago')


def test_a_swap_never_unloads_a_pinned_model():
    """Every other eviction path skips a pinned model; the swap did not, so a
    pinned record still at WARM (before its first tick) could be unloaded."""
    mlm = _mgr()
    _gpu(mlm, 'pinned_voice', 'tts', ModelPriority.WARM, 5000)
    mlm._models['pinned_voice'].pinned = True
    _gpu(mlm, 'speech', 'stt', ModelPriority.WARM, 30)

    assert mlm.request_swap('caption') is True
    mlm._do_unload.assert_called_once_with('speech')


# ── language models and RAM pressure (rv_bdda) ─────────────────────────

def test_gpu_pressure_never_unloads_a_language_model_record(monkeypatch):
    """A language model belongs to llama-server; unloading its record frees
    no VRAM, it only marks the main model unloaded.  The swap skips it, and
    so must the pressure response, even when it is the oldest stamp."""
    mlm = _mgr()
    _gpu(mlm, 'llm-qwen3.5-4b', 'llm', ModelPriority.IDLE, 5000)
    mlm._models['llm-qwen3.5-4b'].pressure_evict_only = True
    _gpu(mlm, 'tts_voice', 'tts', ModelPriority.IDLE, 400)
    pressure = iter([True, True, False])
    monkeypatch.setattr(mlm, '_detect_vram_pressure', lambda: next(pressure))

    mlm._respond_to_vram_pressure()

    assert [c.args[0] for c in mlm._do_unload.call_args_list] == ['tts_voice']


def test_ram_pressure_frees_the_most_evictable_class_first(monkeypatch):
    """_respond_to_ram_pressure sorted by access time alone, so a WARM model
    idle longer went before an EVICTABLE one; it now walks the same order
    as the swap and the GPU response, and skips language-model records.
    The language model is EVICTABLE and idle longest, so the shared order
    alone would pick it first: only the skip keeps it (rv_9baf finding 3)."""
    mlm = _mgr()
    for name, kind, prio, idle in (('llm', 'llm', ModelPriority.EVICTABLE, 9000),
                                   ('warm_older', 'tts', ModelPriority.WARM, 900),
                                   ('ev_newer', 'stt', ModelPriority.EVICTABLE, 10)):
        mlm._models[name] = ModelState(
            name=name, model_type=kind, device=ModelDevice.CPU,
            priority=prio, last_access_time=time.time() - idle)
    pressure = iter([True, False])
    monkeypatch.setattr(mlm, '_detect_ram_pressure', lambda: next(pressure))

    mlm._respond_to_ram_pressure()

    mlm._do_unload.assert_called_once_with('ev_newer')


# ── the last two eviction paths (rv_9baf) ──────────────────────────────

def test_cpu_pressure_never_unloads_a_language_model_record():
    """A chat-only main model registers unpinned and settles at IDLE, the
    class CPU pressure unloads.  Its record frees nothing (llama-server owns
    the process) and only marks the main model unloaded, the false state
    behind the 'Loading tools...' reply to a plain "hi"."""
    mlm = _mgr()
    _gpu(mlm, 'llm-qwen3.5-4b', 'llm', ModelPriority.IDLE, 5000)
    mlm._models['llm-qwen3.5-4b'].pressure_evict_only = True
    _gpu(mlm, 'main_chat', 'llm', ModelPriority.EVICTABLE, 5000)   # typed, not named llm-
    _gpu(mlm, 'speech', 'stt', ModelPriority.IDLE, 400)

    mlm._respond_to_cpu_pressure()

    assert [c.args[0] for c in mlm._do_unload.call_args_list] == ['speech']


def test_the_idle_sweep_never_unloads_a_language_model_record():
    """An admin can set any record's priority to EVICTABLE by hand; the idle
    sweep then reached a language model past its timeout."""
    mlm = _mgr()
    _gpu(mlm, 'llm-qwen3.5-4b', 'llm', ModelPriority.EVICTABLE, 5000)
    _gpu(mlm, 'main_chat', 'llm', ModelPriority.EVICTABLE, 5000)
    _gpu(mlm, 'whisper', 'stt', ModelPriority.EVICTABLE, 5000)

    mlm._evict_idle_models()

    assert [c.args[0] for c in mlm._do_unload.call_args_list] == ['whisper']
