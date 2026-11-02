"""request_swap evicts a GPU model to make room, and never an LLM.

dcc8d3a0 (LLM-safe swap) added `and s.model_type != 'llm'` to the eviction
filter, but ModelState has no model_type field.  Any non-LLM GPU model
reaching that clause raised AttributeError, so request_swap could not swap
at all on a GPU box.  Measured in Nunba's tests/test_model_resilience.py
against HARTOS main (2026-10-23):

    model_lifecycle.py:1992: in request_swap
    AttributeError: 'ModelState' object has no attribute 'model_type'

(4 tests there, including /models/swap answering 500).  No HARTOS test
called request_swap, so it was never caught here.

The LLM guard now asks the module's own name->type map,
ModelLifecycleManager._guess_model_type, which the crash path already uses.

Run:
  pytest tests/unit/test_request_swap_skips_llm.py -v --noconftest
"""
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


@pytest.fixture
def lifecycle():
    from integrations.service_tools.model_lifecycle import (
        ModelDevice,
        ModelLifecycleManager,
        ModelPriority,
        ModelState,
    )
    mgr = ModelLifecycleManager()
    mgr._do_unload = MagicMock()
    mgr._emit_event = MagicMock()

    def add(name, idle_s=300):
        mgr._models[name] = ModelState(
            name=name, device=ModelDevice.GPU, priority=ModelPriority.IDLE,
            last_access_time=time.time() - idle_s)

    mgr.add_gpu_model = add
    return mgr


def test_a_gpu_tts_model_is_evicted_for_the_needed_model(lifecycle):
    lifecycle.add_gpu_model('tts-chatterbox')
    assert lifecycle.request_swap('vlm-minicpm') is True
    lifecycle._do_unload.assert_called_once_with('tts-chatterbox')
    assert lifecycle._swap_queue[-1]['evicted_for'] == 'vlm-minicpm'


def test_a_llama_model_is_never_a_swap_candidate(lifecycle):
    """The guard dcc8d3a0 meant to add: LLMs live in llama-server, so
    evicting their registry entry frees no VRAM."""
    lifecycle.add_gpu_model('llama-main', idle_s=9999)   # oldest, lowest
    assert lifecycle.request_swap('tts-indic-parler') is False
    lifecycle._do_unload.assert_not_called()


def test_the_llm_is_skipped_and_the_other_model_is_evicted(lifecycle):
    lifecycle.add_gpu_model('llama-main', idle_s=9999)
    lifecycle.add_gpu_model('whisper-stt', idle_s=10)
    assert lifecycle.request_swap('tts-indic-parler') is True
    lifecycle._do_unload.assert_called_once_with('whisper-stt')


def test_an_llm_prefixed_entry_is_still_skipped(lifecycle):
    lifecycle.add_gpu_model('llm-qwen3.5-2b', idle_s=9999)
    assert lifecycle.request_swap('tts-indic-parler') is False
