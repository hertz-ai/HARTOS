"""ModelOrchestrator.select_best -- the main LLM's VRAM reserve is taken once.

Measured on the owner's RTX 3070 box, 2026-09-16 (live app, PID 26452):
nvidia-smi free 3.11 GB with llama-server.exe holding 4,833 MB; the live
vram_manager carried ``allocations: {"llm": 2.84}`` (booked by
notify_loaded when llama_config started the server); and yet select_best
answered the STT question with a zero-VRAM entry because its budget was
``3.11 - 4.3 = 0.00``: the ``llm_main`` reserve was subtracted although the
free figure already excluded the resident LLM.  Every non-LLM selector
(TTS, STT, VLM) saw that zero.

The reserve exists for the boot-order case the vram_manager comment
records (2026-08-18: STT selected 6 s before llama-server started and took
the LLM's room).  So: reserve while the LLM is NOT resident; never while it
is.  "Resident" is the row under the orchestrator's LLM key ('llm',
ModelOrchestrator.LLM_VRAM_KEY) in vram_manager.get_allocations() -- the
RAW ledger, not get_allocations_display(), which re-keys rows by catalog
name for the UI.  Booked by notify_loaded here and by Nunba's llama_config
once llama-server passes its health check; released by notify_unloaded,
the lifecycle's dead-process handler, and llama_config on stop.  A reading
older than that booking is re-probed by the ledger revision (8becb7070),
so the free figure the gate sees post-dates the LLM.

Behavioural: real ModelOrchestrator.select_best against a fake catalog that
records the budget it was given.

    python -m pytest tests/unit/test_select_best_llm_reserve.py --noconftest -q
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

FREE_GB = 3.11
RESERVE = (4.3, 2.8)


def _orch():
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    catalog = MagicMock()
    catalog.list_all.return_value = []
    catalog.list_by_type.return_value = []
    catalog.select_best.return_value = None
    orch = ModelOrchestrator(catalog=catalog)
    orch._get_compute_state = lambda: {
        'gpu_available': True, 'gpu_type': 'cuda', 'vram_total_gb': 8.0,
        'vram_free_gb': FREE_GB, 'ram_free_gb': 12.0, 'allocations': {},
    }
    return orch, catalog


def _budget_seen(catalog):
    return catalog.select_best.call_args.kwargs['budget_vram_gb']


def _run(model_type, allocations):
    orch, catalog = _orch()
    vm = MagicMock()
    vm.get_effective_budget.return_value = RESERVE
    vm.get_allocations.return_value = dict(allocations)
    with patch('integrations.service_tools.vram_manager.vram_manager', vm):
        orch.select_best(model_type)
    return _budget_seen(catalog)


def test_reserve_is_not_taken_while_the_llm_is_resident():
    """The live case: free VRAM already excludes the loaded LLM."""
    assert _run('stt', {'llm': 2.84}) == FREE_GB
    assert _run('tts', {'llm': 2.84, 'tts_f5': 1.3}) == FREE_GB


def test_reserve_is_taken_while_the_llm_is_not_resident():
    """The 2026-08-18 case: STT asked before llama-server started."""
    assert _run('stt', {}) == max(0.0, FREE_GB - RESERVE[0])
    assert _run('tts', {'tts_f5': 1.3}) == max(0.0, FREE_GB - RESERVE[0])


def test_the_llm_itself_never_pays_the_reserve():
    assert _run('llm', {}) == FREE_GB
    assert _run('llm', {'llm': 2.84}) == FREE_GB


def test_residency_is_read_from_the_orchestrators_own_llm_key():
    """The key is ModelOrchestrator._vram_key's answer for an LLM entry --
    one vocabulary, not a second literal beside it."""
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    orch, _ = _orch()
    entry = MagicMock()
    entry.model_type = 'llm'
    entry.id = 'llm-qwen3.5-4b-vl-recommended'
    assert orch._vram_key(entry) == ModelOrchestrator.LLM_VRAM_KEY == 'llm'


def test_a_vram_manager_failure_keeps_the_reserve():
    """Unknown residency -> the conservative pre-fix behaviour."""
    orch, catalog = _orch()
    vm = MagicMock()
    vm.get_effective_budget.return_value = RESERVE
    vm.get_allocations.side_effect = RuntimeError('nvml down')
    with patch('integrations.service_tools.vram_manager.vram_manager', vm):
        orch.select_best('stt')
    assert _budget_seen(catalog) == max(0.0, FREE_GB - RESERVE[0])
