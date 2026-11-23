"""A voice installed outside the orchestrator clears its remembered failure.

MEASURED 2026-11-18 by rv_b9 (D:/claude_scratch/99c39a1e/rv_b9/probes/
manual_install_clears.py, on HARTOS 4fa1c8a30): after a failed setup was
written on tts-neutts-air, Nunba's install route reported the successful
reinstall with notify_downloaded('tts', 'neutts_air'), and the failure was
still held, so the voice stayed out of select_best for up to 24 h.  Two
reasons: 'neutts_air' matched no row (the row id joins its words with '-'),
and a row already downloaded was skipped.
"""
import pytest

from integrations.service_tools.model_catalog import ModelCatalog, ModelEntry, ModelType
from integrations.service_tools.model_orchestrator import ModelOrchestrator


@pytest.fixture
def orch(tmp_path):
    cat = ModelCatalog(catalog_path=str(tmp_path / 'c.json'))
    o = ModelOrchestrator(catalog=cat)

    def failed(mid, name, downloaded):
        cat.register(ModelEntry(id=mid, name=name, model_type=ModelType.TTS,
                                supports_cpu=True), persist=False)
        cat.get(mid).downloaded = downloaded
        cat.mark_setup_failed(mid, 'pip install failed: could not reach the index')
        assert cat.get(mid).setup_failure_current()
    return o, cat, failed


@pytest.mark.parametrize('downloaded', [False, True])
def test_a_reinstall_by_engine_id_clears_the_failure(orch, downloaded):
    o, cat, failed = orch
    failed('tts-neutts-air', 'NeuTTS Air', downloaded)
    o.notify_downloaded(ModelType.TTS, 'neutts_air')
    row = cat.get('tts-neutts-air')
    assert row.downloaded is True
    assert not row.setup_failure_current()


def test_the_engine_id_finds_its_own_row_not_a_longer_one(orch):
    o, cat, failed = orch
    failed('tts-chatterbox-ml', 'Chatterbox Multilingual', False)
    failed('tts-chatterbox-turbo', 'Chatterbox Turbo', False)
    o.notify_downloaded(ModelType.TTS, 'chatterbox_turbo')
    assert not cat.get('tts-chatterbox-turbo').setup_failure_current()
    assert cat.get('tts-chatterbox-ml').setup_failure_current()


def test_an_older_name_that_matched_before_still_matches(orch):
    o, cat, failed = orch
    failed('tts-f5-tts', 'F5-TTS', False)
    o.notify_downloaded(ModelType.TTS, 'f5')
    assert not cat.get('tts-f5-tts').setup_failure_current()


def test_a_downloaded_row_with_no_failure_is_left_alone(orch):
    o, cat, _ = orch
    cat.register(ModelEntry(id='tts-kokoro', name='Kokoro', model_type=ModelType.TTS),
                 persist=False)
    cat.get('tts-kokoro').downloaded = True
    calls = []
    cat.mark_downloaded = lambda *a, **k: calls.append(a)
    o.notify_downloaded(ModelType.TTS, 'kokoro')
    assert calls == []


def test_another_type_still_matches_by_its_name(orch):
    o, cat, _ = orch
    cat.register(ModelEntry(id='stt-whisper-base', name='Whisper Base',
                            model_type=ModelType.STT), persist=False)
    o.notify_downloaded(ModelType.STT, 'whisper-base')
    assert cat.get('stt-whisper-base').downloaded is True
