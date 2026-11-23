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


def _gpu(monkeypatch, cuda, free_gb):
    """Set the machine through vram_manager's own detection boundary, so the
    booking does not depend on this desktop's real card or its free memory
    at run time (rv_old2's review of 5f466da7a: the test passed only on a GPU
    with at least 0.7 GB free). No measured sizes, so the declared budget row
    is the one read."""
    monkeypatch.setattr(vram_manager, 'detect_gpu', lambda: {
        'name': 'test card' if cuda else None, 'total_gb': 8.0 if cuda else 0.0,
        'free_gb': free_gb, 'cuda_available': cuda})
    monkeypatch.setattr(vram_manager, '_measured', {})


def _book(o, entry):
    key = o._vram_key(entry)
    vram_manager._allocations.pop(key, None)
    try:
        o._register_vram(entry, 'gpu')
        return key, vram_manager._allocations.get(key)
    finally:
        vram_manager._allocations.pop(key, None)


def test_neutts_books_its_budget_not_zero(orch, monkeypatch):
    """The defect was 0.0 booked under the catalog id. A GPU booking reserves
    the budget row's model size, the one figure allocate books: the row is
    (min_vram_gb, model_size_gb), and min_vram is only the room it needs."""
    o, cat = orch
    _gpu(monkeypatch, cuda=True, free_gb=6.0)
    key, booked = _book(o, _voice(cat, 'tts-neutts-air'))
    assert key == 'tts_neutts'
    assert booked is not None and booked > 0, booked   # a real booking, not 0.0
    assert booked == vram_manager.get_effective_budget('tts_neutts')[1]
    assert booked == VRAM_BUDGETS['tts_neutts'][1]


def test_a_card_without_room_or_no_card_books_nothing(orch, monkeypatch):
    """allocate refuses a GPU booking that does not fit (can_fit: free below
    min_vram, or no GPU at all); nothing is reserved, never a made-up figure."""
    o, cat = orch
    for cuda, free in ((True, 0.5), (False, 0.0)):
        _gpu(monkeypatch, cuda=cuda, free_gb=free)
        assert _book(o, _voice(cat, 'tts-neutts-air'))[1] is None, (cuda, free)


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
