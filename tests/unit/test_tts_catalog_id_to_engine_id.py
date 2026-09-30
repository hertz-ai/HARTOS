"""tts_router.catalog_id_to_engine_id is THE catalog-id -> engine-name rule.

Public because Nunba converts the same ids (tts_engine.catalog_entry_backend
and TTSLoader call it); Nunba's own copy, ``replace('tts-', '', 1)``, kept
the dashes and named a different engine for every multi-word id it did not
map.  Nunba's tests/test_tts_catalog_id_one_rule.py drives Nunba's call
sites against this function; these pin the function itself.
"""
import pytest

from integrations.channels.media import tts_router as tr


@pytest.mark.parametrize('engine_id', list(tr.ENGINE_REGISTRY))
def test_every_registry_id_round_trips(engine_id):
    cid = tr._engine_id_to_catalog_id(engine_id)
    assert cid.startswith('tts-') and '_' not in cid
    assert tr.catalog_id_to_engine_id(cid) == engine_id


@pytest.mark.parametrize('cid,engine_id', [
    ('tts-future-voice-x', 'future_voice_x'),   # unmapped, multi-word
    ('tts-xtts-v2', 'xtts_v2'),                 # 'tts-' inside, not a prefix
    ('xtts-v2', 'xtts_v2'),                     # no prefix at all
    ('tts-kokoro', 'kokoro'),
])
def test_rule(cid, engine_id):
    assert tr.catalog_id_to_engine_id(cid) == engine_id


def test_private_name_is_gone():
    """One name for the rule: a private twin is what Nunba reached into."""
    assert not hasattr(tr, '_catalog_id_to_engine_id')
