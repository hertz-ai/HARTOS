"""The chat model's room on the GPU is held back once, by one reader.

Three live cases this pins:

2026-08-18: STT was selected 6 s before llama-server started and took the
chat model's room; the model then came up with a 4096 context.

2026-09-16 (PID 26452): free 3.11 GB with llama-server holding 4.83 GB and
the ledger at {"llm": 2.84}; select_best still subtracted 4.3 and answered
every STT/TTS/VLM question with 0.00.

2026-11-28 19:51 IST: load() wrote 'llm': 0.0 eleven seconds before the
server started, select_best read that as "resident" and dropped the
reserve, and can_fit never had one: a 3.8 GB voice went "fit=gpu" on an
empty 8 GB card and the two llama-servers loaded on top of it.

The rule: vram_manager.llm_reserve_gb() is the llm_main min_vram while the
chat model was decided for the GPU (expect_llm_on_gpu) and its server has
not booked the 'llm' row; zero otherwise, zero where nobody said a chat
model is coming (central, a remote chat model).  can_fit and
ModelOrchestrator.select_best both read it.

Real VRAMManager and real ModelOrchestrator.select_best; only the GPU
probe and the catalog are stand-ins.

    python -m pytest tests/unit/test_select_best_llm_reserve.py --noconftest -q
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The package exports the manager instance under the module's own name, so
# the module is reached by its dotted path.
_VM = 'integrations.service_tools.vram_manager'

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


def _manager(free_gb=FREE_GB, cuda=True):
    vmod = importlib.import_module(_VM)
    vm = vmod.VRAMManager()
    vm._measured = {}
    vm.get_free_vram = lambda: free_gb
    vm.detect_gpu = lambda: {'cuda_available': cuda, 'metal_available': False}
    return vm


def _run(model_type, allocations, placement='pending'):
    orch, catalog = _orch()
    vm = _manager()
    if placement == 'pending':
        vm.expect_llm_on_gpu()
    elif placement == 'cpu':
        vm.llm_not_on_gpu()
    for key, gb in allocations.items():
        vm._allocations[key] = gb
    with patch('integrations.service_tools.vram_manager.vram_manager', vm):
        orch.select_best(model_type)
    return _budget_seen(catalog)


HELD = max(0.0, FREE_GB - RESERVE[0])


def test_the_reserve_is_the_budget_rows_own_figure():
    assert importlib.import_module(_VM).VRAM_BUDGETS['llm_main'] == RESERVE


def test_no_reserve_once_the_server_booked_its_row():
    assert _run('stt', {'llm': 2.84}) == FREE_GB
    assert _run('tts', {'llm': 2.84, 'tts_f5': 1.3}) == FREE_GB
    assert _run('tts', {'llm': 0.52}) == FREE_GB


def test_reserve_while_the_chat_model_is_on_its_way():
    assert _run('stt', {}) == HELD
    assert _run('tts', {'tts_f5': 1.3}) == HELD


def test_a_row_that_books_nothing_is_not_a_loaded_model():
    assert _run('tts', {'llm': 0.0}) == HELD
    assert _run('stt', {'llm': 0.0, 'tts_f5': 1.3}) == HELD
    assert _run('tts', {'llm': None}) == HELD


def test_the_chat_model_never_pays_for_its_own_room():
    assert _run('llm', {}) == FREE_GB
    assert _run('llm', {'llm': 2.84}) == FREE_GB


def test_no_reserve_where_nobody_said_a_chat_model_is_coming():
    """Central and a node whose chat model is remote: nothing to hold."""
    assert _run('tts', {}, placement=None) == FREE_GB
    assert _run('stt', {'llm': 0.0}, placement=None) == FREE_GB


def test_no_reserve_for_a_chat_model_on_the_cpu():
    assert _run('tts', {}, placement='cpu') == FREE_GB


def test_a_start_that_was_given_up_stops_holding_room():
    vm = _manager()
    vm.expect_llm_on_gpu()
    assert vm.llm_reserve_gb() == RESERVE[0]
    vm.llm_placement_unknown()
    assert vm.llm_reserve_gb() == 0.0


def test_the_ledger_key_is_the_orchestrators_own():
    vmod = importlib.import_module(_VM)
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    orch, _ = _orch()
    entry = MagicMock()
    entry.model_type = 'llm'
    entry.id = 'llm-qwen3.5-4b-vl-recommended'
    assert (orch._vram_key(entry) == ModelOrchestrator.LLM_VRAM_KEY
            == vmod.LLM_LEDGER_KEY)


# ── can_fit: the 19:51 boot order ─────────────────────────────────────

def test_a_voice_asking_first_is_told_the_room_is_spoken_for():
    """7.83 GB free, chat model decided for the GPU and not up: a voice
    that needs 3.8 GB does not fit beside the 4.3 GB held for it."""
    vmod = importlib.import_module(_VM)
    vm = _manager(free_gb=7.83)
    with patch.dict(vmod.VRAM_BUDGETS, {'a_voice': (3.8, 3.8)}):
        assert vm.can_fit('a_voice') is True          # nobody said anything
        vm.expect_llm_on_gpu()
        assert vm.can_fit('a_voice') is False
        assert vm.allocate('a_voice') is False
        vm._allocations['llm'] = 2.84                 # server up, row booked
        assert vm.can_fit('a_voice') is True


def test_a_small_voice_still_fits_beside_the_held_room():
    vmod = importlib.import_module(_VM)
    vm = _manager(free_gb=7.83)
    vm.expect_llm_on_gpu()
    with patch.dict(vmod.VRAM_BUDGETS, {'a_voice': (2.0, 1.5)}):
        assert vm.can_fit('a_voice') is True


def test_the_chat_models_own_keys_see_no_reserve_in_can_fit():
    vm = _manager(free_gb=4.5)
    vm.expect_llm_on_gpu()
    assert vm.can_fit('llm_main') is True
    assert vm.can_fit('llm') is True


def test_a_tool_already_booked_is_not_asked_again():
    vmod = importlib.import_module(_VM)
    vm = _manager(free_gb=1.0)
    vm._allocations['a_voice'] = 3.8
    vm.expect_llm_on_gpu()
    with patch.dict(vmod.VRAM_BUDGETS, {'a_voice': (3.8, 3.8)}):
        assert vm.can_fit('a_voice') is True
