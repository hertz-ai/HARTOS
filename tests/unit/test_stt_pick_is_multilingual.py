"""The live STT pick is never an English-only model (owner 2026-11-24: "transcription
shd be language agnostic"; owner 2026-11-25: the child persona speaks Tamil).

Live 2026-11-25 12:28 IST on the installed desktop: Whisper small heard Tamil
(detected 'ta' 0.76), then _catalog_stt_entry's select_best('stt') chose
stt-sherpa-moonshine-tiny, English-only but already loaded (+1000 for loaded,
score 1362 against Whisper small's 380). Moonshine wrote the Tamil as English
words ("The middle river, it's standing straight") and the assistant answered in
English.

    venv/Scripts/python.exe -m pytest tests/unit/test_stt_pick_is_multilingual.py -q --noconftest
"""
from unittest import mock


def _catalog(tmp_path):
    from integrations.service_tools.model_catalog import ModelCatalog
    from integrations.service_tools.whisper_tool import populate_stt_catalog
    cat = ModelCatalog(catalog_path=str(tmp_path / 'catalog.json'))
    populate_stt_catalog(cat)
    moon = cat.get('stt-sherpa-moonshine-tiny')
    moon.loaded = True           # the state on the owner's desktop at 12:28
    moon.downloaded = True
    small = cat.get('stt-faster-whisper-small')
    small.downloaded = True
    return cat


def test_the_live_pick_asks_for_a_multilingual_model():
    from integrations.service_tools import whisper_tool
    seen = {}

    class _Orch:
        def select_best(self, model_type, **kw):
            seen['model_type'] = model_type
            seen.update(kw)
            return None

    with mock.patch('integrations.service_tools.model_orchestrator.get_orchestrator',
                    return_value=_Orch()):
        whisper_tool._catalog_stt_entry()
    assert seen['model_type'] == 'stt'
    assert seen.get('require_capability') == {'multilingual': True}


def test_a_loaded_english_only_model_loses_to_whisper(tmp_path):
    cat = _catalog(tmp_path)
    pick = cat.select_best('stt', budget_vram_gb=1.0, budget_ram_gb=8,
                           gpu_available=True,
                           require_capability={'multilingual': True})
    assert pick is not None
    assert pick.capabilities.get('multilingual') is True
    assert 'moonshine' not in pick.id


def test_without_the_requirement_the_loaded_english_model_wins(tmp_path):
    """Pins the defect's mechanism: the loaded bonus alone hands the pick to
    Moonshine, so the requirement above is what keeps Tamil transcribable."""
    cat = _catalog(tmp_path)
    pick = cat.select_best('stt', budget_vram_gb=1.0, budget_ram_gb=8,
                           gpu_available=True)
    assert pick is not None and pick.id == 'stt-sherpa-moonshine-tiny'
