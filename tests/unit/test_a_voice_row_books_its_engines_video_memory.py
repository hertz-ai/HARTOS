"""A voice engine's row carries its engine's real video memory, also on an
existing install.

MEASURED 2026-11-18 (repro/probe_hartos.py on HARTOS ad16533dd):
_engine_vram_gb guessed the budget key as 'tts_' + engine id, so F5
('tts_f5_tts') and NeuTTS ('tts_neutts_air') found no budget and read 0.0 GB
while VRAM_BUDGETS holds them as 'tts_f5' (2.5 GB) and 'tts_neutts' (0.7 GB),
the keys their own specs declare.  A row that costs 0.0 GB is picked as if it
were free, then does not fit beside the 4B.

And the fix alone would not reach the owner's desktop: populate_tts_catalog
skipped every row that already existed, so the rows written 2026-09-23 kept
0.0 GB forever, and ENGINE_REGISTRY is then rebuilt FROM those rows.  An
existing shipped row now takes the seed's vram, ram, languages, language
priority and vram_key, as the LLM seed does; the owner's flags stay.
"""
import pytest

from integrations.channels.media import tts_router as tr
from integrations.service_tools.model_catalog import ModelCatalog, ModelEntry


@pytest.fixture
def fresh(monkeypatch):
    """No cached VRAM answers, and ENGINE_REGISTRY restored afterwards
    (populate_tts_catalog rebuilds it in place)."""
    monkeypatch.setattr(tr, '_ENGINE_VRAM_GB', {})
    saved = dict(tr.ENGINE_REGISTRY)
    yield
    tr.ENGINE_REGISTRY.clear()
    tr.ENGINE_REGISTRY.update(saved)


def test_f5_books_its_own_budget(fresh):
    assert tr._engine_vram_gb('f5_tts') == 2.5


def test_neutts_books_its_own_budget(fresh):
    assert tr._engine_vram_gb('neutts_air') == 0.7


def test_an_engine_whose_key_follows_the_old_rule_is_unchanged(fresh):
    from integrations.service_tools.vram_manager import VRAM_BUDGETS
    assert tr._SEED_SPECS['kokoro'].vram_key == 'tts_kokoro'
    assert tr._engine_vram_gb('kokoro') == VRAM_BUDGETS['tts_kokoro'][0]


def _stale_install(path):
    """The owner's catalog as written 2026-09-23: a real seeded row, saved,
    then carrying what an older seed wrote (NeuTTS at 0.0 GB, a wrong
    language list, the old vram_key) and the owner's own choice to keep it
    off; read back by a new catalog, as the next boot does."""
    cat = ModelCatalog(catalog_path=str(path))
    tr.populate_tts_catalog(cat)
    row = cat.get('tts-neutts-air')
    cat.override('tts-neutts-air', persist=False, vram_gb=0.0, languages=['xx'],
                 enabled=False,
                 capabilities={**(row.capabilities or {}), 'vram_key': 'tts_neutts_air'})
    cat._save()
    return ModelCatalog(catalog_path=str(path))


def test_an_existing_row_takes_the_seeds_video_memory(tmp_path, fresh):
    cat = _stale_install(tmp_path / 'c.json')
    tr.populate_tts_catalog(cat)
    row = cat.get('tts-neutts-air')
    assert row.vram_gb == 0.7
    assert row.capabilities['vram_key'] == 'tts_neutts'


def test_an_existing_row_takes_the_seeds_languages_and_the_registry_follows(tmp_path, fresh):
    seed = tr._SEED_SPECS['neutts_air']
    cat = _stale_install(tmp_path / 'c.json')
    tr.populate_tts_catalog(cat)
    assert cat.get('tts-neutts-air').languages == list(seed.languages)
    assert list(tr.ENGINE_REGISTRY['neutts_air'].languages) == list(seed.languages)


def test_the_owners_choice_to_keep_a_voice_off_survives(tmp_path, fresh):
    cat = _stale_install(tmp_path / 'c.json')
    tr.populate_tts_catalog(cat)
    assert cat.get('tts-neutts-air').enabled is False


def test_a_second_populate_still_adds_nothing(tmp_path, fresh):
    cat = _stale_install(tmp_path / 'c.json')
    tr.populate_tts_catalog(cat)
    assert tr.populate_tts_catalog(cat) == 0
