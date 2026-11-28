"""mms_tts is offered only for a language it has a model for.

WHAT WENT WRONG (live, 2026-11-28 12:55 IST, installed Nunba)
  One Japanese line picked mms_tts.  Its worker started cold (15 s) and then
  failed: "facebook/mms-tts-jpn is not a local folder and is not a valid
  model identifier".  MMS has no Japanese model.

WHAT WAS MEASURED (2026-11-28)
  A header-only request for
  https://huggingface.co/facebook/mms-tts-<code>/resolve/main/config.json
  for every code in ISO1_TO_ISO3: 307 means the model is there, 401 means
  there is no such model.
    * 40 codes answered 307.
    * 8 languages have no model: it (ita), cs (ces), zh (cmn), ja (jpn),
      ne (nep, npi), sd (snd), sa (san), si (sin).
    * 2 codes were wrong: fa was 'pes' (401), 'fas' is 307; ur was 'urd'
      (401), 'urd-script_arabic' is 307.

THE RULE
  The table in mms_tts_tool holds only codes measured as there.  The router
  names mms_tts for a language only when the table has that language: in the
  engine's own language list and in every per-language ladder.

    python -m pytest tests/unit/test_mms_speaks_only_the_languages_it_has_a_model_for.py -q
"""
import pytest

from integrations.channels.media.tts_router import (
    ENGINE_REGISTRY, LANG_ENGINE_PREFERENCE)
from integrations.service_tools.mms_tts_tool import (
    ISO1_TO_ISO3, _iso1_to_iso3)

# Codes whose facebook/mms-tts-<code> answered 307 on 2026-11-28.
MODELS_THAT_EXIST = frozenset({
    'eng', 'spa', 'fra', 'deu', 'por', 'nld', 'pol', 'tur', 'rus', 'hun',
    'swe', 'fin', 'ell', 'ron', 'bul', 'ukr', 'cym', 'isl', 'kor', 'vie',
    'tha', 'ind', 'zlm', 'khm', 'lao', 'mya', 'hin', 'ben', 'tam', 'tel',
    'mar', 'guj', 'kan', 'mal', 'pan', 'ory', 'asm', 'ara', 'heb', 'swh',
    'fas', 'urd-script_arabic', 'lav', 'mai',
})

# Languages whose every candidate code answered 401 on 2026-11-28
# (Serbian srp and Kashmiri kas among them).
NO_MODEL = ('it', 'cs', 'zh', 'ja', 'ne', 'sd', 'sa', 'si', 'sr', 'ks')


def _table_language(row_key):
    """The table key a ladder row's language is looked up by, the way the
    tool does it ('zh-cn' is asked for as 'zh')."""
    return row_key.replace('_', '-').split('-')[0].lower()


def test_every_code_in_the_table_is_a_model_that_exists():
    unknown = {lang: code for lang, code in ISO1_TO_ISO3.items()
               if code not in MODELS_THAT_EXIST}
    assert not unknown, (
        f'codes not measured as there on 2026-11-28: {unknown}')


@pytest.mark.parametrize('lang', NO_MODEL)
def test_a_language_with_no_model_is_not_in_the_table(lang):
    assert lang not in ISO1_TO_ISO3
    assert _iso1_to_iso3(lang) is None


def test_persian_and_urdu_use_the_codes_that_exist():
    assert _iso1_to_iso3('fa') == 'fas'
    assert _iso1_to_iso3('ur') == 'urd-script_arabic'


def test_no_ladder_names_mms_for_a_language_outside_the_table():
    wrong = sorted(
        lang for lang, ladder in LANG_ENGINE_PREFERENCE.items()
        if 'mms_tts' in ladder and _table_language(lang) not in ISO1_TO_ISO3)
    assert not wrong, (
        f'ladders that name mms_tts for a language it has no model for: '
        f'{wrong}')


def test_the_engine_lists_only_languages_in_the_table():
    listed = set(ENGINE_REGISTRY['mms_tts'].languages)
    assert listed <= set(ISO1_TO_ISO3), sorted(listed - set(ISO1_TO_ISO3))


@pytest.mark.parametrize('lang', NO_MODEL)
def test_no_ladder_names_mms_for_a_language_with_no_model(lang):
    assert 'mms_tts' not in LANG_ENGINE_PREFERENCE.get(lang, ())
    assert lang not in ENGINE_REGISTRY['mms_tts'].languages


def test_latvian_and_maithili_use_the_models_that_exist():
    """Both answered 307 on 2026-11-28 and were left out of the first fix
    (3c56fa0c6), so two languages MMS can speak were not offered it."""
    assert _iso1_to_iso3('lv') == 'lav'
    assert _iso1_to_iso3('mai') == 'mai'


@pytest.mark.parametrize('lang', ('ta', 'hi', 'en', 'fa', 'ur', 'lv', 'mai'))
def test_a_language_with_a_model_is_still_offered(lang):
    assert lang in ISO1_TO_ISO3
    assert lang in ENGINE_REGISTRY['mms_tts'].languages
    assert 'mms_tts' in LANG_ENGINE_PREFERENCE[lang]
