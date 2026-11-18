"""The orchestrator books a voice model under its engine's own VRAM key.

MEASURED 2026-11-18 (repro/critic_probe.py [orch_tts_vram_key] on HARTOS
2dbe65d24): _CATALOG_TO_VRAM_KEY lists six voice rows; any other voice row
(NeuTTS, MeloTTS, XTTS, MMS, OmniVoice) fell back to its catalog id, which has
no budget, so a load booked 0.0 GB under 'tts-neutts-air' beside the voice
worker's real 'tts_neutts' row.  The swap planner then believed the card had
memory it did not.

Now a voice row the map does not list books under its engine spec's vram_key,
the same key the worker and Nunba book; listed rows and other types are as
before.
"""
import pytest

from integrations.service_tools.model_catalog import ModelCatalog, ModelEntry, ModelType
from integrations.service_tools.model_orchestrator import ModelOrchestrator
from integrations.service_tools.vram_manager import vram_manager, VRAM_BUDGETS


@pytest.fixture
def orch(tmp_path):
    cat = ModelCatalog(catalog_path=str(tmp_path / 'c.json'))
    return ModelOrchestrator(catalog=cat), cat


def _voice(cat, mid):
    cat.register(ModelEntry(id=mid, name=mid, model_type=ModelType.TTS,
                            vram_gb=0.7, supports_gpu=True), persist=False)
    return cat.get(mid)


@pytest.mark.parametrize('mid,key', [
    ('tts-neutts-air', 'tts_neutts'),
    ('tts-melotts', 'tts_melotts'),
    ('tts-xtts-v2', 'tts_xtts_v2'),
    ('tts-mms-tts', 'tts_mms_tts'),
    ('tts-omnivoice', 'tts_omnivoice'),
])
def test_an_unlisted_voice_row_uses_its_engines_key(orch, mid, key):
    o, cat = orch
    assert o._vram_key(_voice(cat, mid)) == key


def test_a_cpu_voice_with_no_engine_key_falls_back_to_its_id(orch):
    o, cat = orch
    assert o._vram_key(_voice(cat, 'tts-piper')) == 'tts-piper'


def test_neutts_books_its_budget_not_zero(orch):
    o, cat = orch
    entry = _voice(cat, 'tts-neutts-air')
    key = o._vram_key(entry)
    vram_manager._allocations.pop(key, None)
    try:
        o._register_vram(entry, 'gpu')
        assert key == 'tts_neutts'
        assert vram_manager._allocations.get(key) == VRAM_BUDGETS['tts_neutts'][0]
    finally:
        vram_manager._allocations.pop(key, None)


def test_a_listed_voice_row_keeps_its_mapped_key(orch):
    o, cat = orch
    assert o._vram_key(_voice(cat, 'tts-f5-tts')) == 'tts_f5'


def test_a_row_of_another_type_still_falls_back_to_its_id(orch):
    o, cat = orch
    cat.register(ModelEntry(id='vlm-something-new', name='x', model_type=ModelType.VLM),
                 persist=False)
    assert o._vram_key(cat.get('vlm-something-new')) == 'vlm-something-new'


def test_the_main_llm_keeps_its_one_row(orch):
    o, cat = orch
    cat.register(ModelEntry(id='llm-any', name='x', model_type=ModelType.LLM), persist=False)
    assert o._vram_key(cat.get('llm-any')) == ModelOrchestrator.LLM_VRAM_KEY
