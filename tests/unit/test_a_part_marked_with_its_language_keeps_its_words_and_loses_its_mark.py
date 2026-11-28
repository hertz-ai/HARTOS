"""A part marked with its language: a person reads the words, never the mark.

Speech design Part A (D:\\claude_scratch\\66e67cad\\speech_out_1128\\design.md,
review 1 MUST 1, 2 and 6; owner 28 Nov 2026: the model tags a language the
way it tags singing).  The model will mark a part written in another language
as <es>...</es> (or <lang es>...</lang>).  This file pins the reading half,
which ships before any model is told about the marks:

1. Read clean: the one function that takes <sing> out of every text a person
   reads (words_without_media_tags) takes the language marks out too, words
   kept.  Tags of a web page (<b>, <br>, <em> ...) are left as written.
2. Spoken whole: the speech normaliser hands every mark on, untouched, so the
   Nunba segmenter can speak each part in its language.  A sung part left
   open loses its sing tag and keeps its language mark.
3. One grammar: tests/fixtures/speech_tags_cases.json is the same file in
   Nunba-HART-Companion, where the segmenter is run against it; its sha256 is
   pinned in both repos.
4. The recorded-voice path (TTSRouter.synthesize: one engine, no segmenter)
   speaks a reply marked whole in the marked language, and never a mark.

Real: words_without_media_tags, language_marked_on_every_word,
normalize_for_tts, TTSRouter.synthesize, publish_agent_message.
Faked: the engine choice (recorded), the realtime publisher (recorded), the
normaliser's cache (off).

    python -m pytest tests/unit/test_a_part_marked_with_its_language_keeps_its_words_and_loses_its_mark.py -q
"""
import hashlib
import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.constants import SUPPORTED_LANG_DICT  # noqa: E402
from integrations.channels.media import tts_text_normalizer as norm  # noqa: E402
from integrations.channels.media.tts_text_normalizer import (  # noqa: E402
    language_marked_on_every_word, words_without_media_tags)

FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'speech_tags_cases.json')
# The same constant is in Nunba-HART-Companion
# tests/test_a_part_marked_with_its_language_is_spoken_in_it.py.
FIXTURE_SHA256 = '173e565573587d4515aedd56a09217059455e529d301e715f33c4a46cbc2bde9'

with open(FIXTURE, 'rb') as _f:
    # line ends as git stores them, whatever the checkout wrote
    _BYTES = _f.read().replace(b'\r\n', b'\n')
_SHARED = json.loads(_BYTES.decode('utf-8'))
CASES = _SHARED['cases']
_IDS = [c['name'] for c in CASES]


@pytest.fixture
def no_cache(monkeypatch):
    """The normaliser's cache is a file under the person's home; off here."""
    monkeypatch.setattr(norm, '_cache_get', lambda *a: None)
    monkeypatch.setattr(norm, '_cache_put', lambda *a: None)


# 3. One grammar --------------------------------------------------------------

def test_the_shared_cases_are_the_same_file_as_in_nunba():
    assert hashlib.sha256(_BYTES).hexdigest() == FIXTURE_SHA256, (
        'tests/fixtures/speech_tags_cases.json changed: make the same change '
        'in Nunba-HART-Companion and set the new sha256 in both tests')


def test_a_bare_mark_is_a_language_this_system_knows():
    ours = {code.split('-')[0].lower() for code in SUPPORTED_LANG_DICT}
    assert ours == set(_SHARED['bare_codes']), (
        'SUPPORTED_LANG_DICT and the shared list differ: a language added '
        'here goes into the fixture, and into Nunba LANG_SCRIPTS',
        sorted(ours ^ set(_SHARED['bare_codes'])))


# 1. What a person reads ------------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=_IDS)
def test_a_person_reads_the_words_and_never_a_language_mark(case):
    assert words_without_media_tags(case['input']) == case['shown']


@pytest.mark.parametrize('text', [
    '<b>bold</b> is not ours', 'line one<br>line two', '<i>x</i> <p>y</p>',
    '<a>link</a> and <em>stress</em>', '<div>box</div> <pre>code</pre>',
    'git push origin <sha>:main', 'a < es > b', '<ES>Hola</ES>',
    '<es lang>Hola', 'if a <b and c> d'])
def test_tags_that_are_not_language_marks_come_back_exactly_as_they_came(text):
    assert words_without_media_tags(text) is text


class TestASendIsReadWithoutItsLanguageMark:
    """One real surface end to end: a mid-turn send.  Every other text a
    person reads goes through the same function (the sing tests pin each)."""

    def setup_method(self):
        from core.peer_link import crossbar_publish as cp
        self.cp = cp
        self._tmpdir = tempfile.mkdtemp()
        self._env = patch.dict(os.environ, {'HEVOLVE_CACHE_DIR': self._tmpdir})
        self._env.start()
        self.published = []
        self._attr = patch(
            'core.safe_hartos_attr.safe_hartos_attr',
            lambda name: (lambda topic, payload, **flags:
                          self.published.append(payload))
            if name == 'publish_async' else None)
        self._attr.start()
        self._kept = patch.dict(getattr(cp, '_turn_sends', {}), {}, clear=True)
        self._kept.start()

    def teardown_method(self):
        self._kept.stop()
        self._attr.stop()
        self._env.stop()

    def test_the_shown_text_has_no_mark_and_the_turns_record_keeps_it(self):
        turn = '7c2e4d1a-9b3f-4c6e-8a2d-1f0e5b7c9d33'
        said = 'In Spanish: <es>Hola amigo</es>'
        assert self.cp.publish_agent_message(
            text=said, user_id='u1', prompt_id=7,
            request_id=f'{turn}-intermediate-1')
        shown = [p for p in self.published if isinstance(p, dict) and 'text' in p]
        assert shown and shown[-1]['text'] == ['In Spanish: Hola amigo']
        assert said in self.cp.turn_sends(turn), (
            'the record the reply is spoken from keeps the mark for the speaker')


# 2. What is spoken -----------------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=_IDS)
def test_every_mark_reaches_the_speaker_as_written(case, no_cache):
    """All the shared cases have whole sung parts, so nothing is cut."""
    out = norm.normalize_for_tts(case['input'], case['language'], use_llm=False)
    assert norm._MEDIA_TAG.findall(out) == norm._MEDIA_TAG.findall(case['input'])


def test_the_words_inside_a_marked_part_are_still_made_sayable(no_cache):
    assert norm.normalize_for_tts(
        'It costs <es>$5 hoy</es>', 'en', use_llm=False) == (
        'It costs <es>5 dollars hoy</es>')


def test_a_sung_part_left_open_keeps_its_language_mark(no_cache):
    """The sing tag would be read aloud, so it goes; the language mark is
    read by the segmenter open or closed, so it stays."""
    assert norm.normalize_for_tts(
        '<sing>la la <es>hola amigo</es>', 'en', use_llm=False) == (
        'la la <es>hola amigo</es>')
    assert norm.normalize_for_tts(
        '<es>hola <sing>la la', 'en', use_llm=False) == '<es>hola la la'


def test_a_language_mark_left_open_is_not_a_broken_sung_part(no_cache):
    out = norm.normalize_for_tts(
        '<ta>vanakkam <sing>nila nila</sing>', 'en', use_llm=False)
    assert '<sing>nila nila</sing>' in out and out.startswith('<ta>'), out


def test_the_model_rewrite_never_sees_a_language_mark(monkeypatch, no_cache):
    seen = []
    monkeypatch.setattr(norm, '_llm_normalize',
                        lambda text, lang, budget=None: seen.append(text) or text.upper())
    monkeypatch.setattr(norm, '_has_residual_tokens',
                        lambda t: any(ch.isalpha() for ch in t))
    out = norm.normalize_for_tts(
        'Say it. <lang es>Hola amigo</lang> Bye', 'en', use_llm=True)
    assert len(seen) == 3 and all('<' not in t and '>' not in t for t in seen), seen
    assert '<lang es>HOLA AMIGO</lang>' in out, out


# 4. One engine, no segmenter -------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=_IDS)
def test_the_one_language_every_word_is_marked_with(case):
    assert language_marked_on_every_word(case['input']) == case['whole_language']


@pytest.mark.parametrize('text', ['plain words', '', None, 'a < b'])
def test_text_with_no_mark_names_no_language(text):
    assert language_marked_on_every_word(text) is None


def _what_the_router_speaks(text, language):
    """(text, language) the router chooses an engine for."""
    from integrations.channels.media import tts_router
    router = tts_router.TTSRouter()
    spoken = []

    def _select(said, lang, *a, **k):
        spoken.append((said, lang))
        return []

    with patch.object(norm, '_cache_get', lambda *a: None), \
            patch.object(norm, '_cache_put', lambda *a: None), \
            patch.object(router, 'select_engines', side_effect=_select):
        router.synthesize(text, language=language, voice='my_voice',
                          source='chat_response')
    assert spoken, 'the router never reached engine selection'
    return spoken[0]


def test_the_recorded_voice_speaks_a_reply_marked_whole_in_its_language():
    assert _what_the_router_speaks(
        '<es>Hola amigo, vamos al parque</es>', 'en') == (
        'Hola amigo, vamos al parque', 'es')


def test_the_recorded_voice_speaks_a_sung_spanish_reply_in_spanish():
    assert _what_the_router_speaks(
        '<sing><es>la cucaracha</es></sing>', 'en') == ('la cucaracha', 'es')


def test_the_recorded_voice_never_says_a_mark_of_a_reply_marked_in_parts():
    """One engine says the whole text: it keeps the language asked for, and
    the marks are gone."""
    assert _what_the_router_speaks(
        'In Spanish: <es>Hola amigo</es>', 'en') == (
        'In Spanish: Hola amigo', 'en')


def test_the_recorded_voice_keeps_the_language_asked_for_with_no_mark():
    assert _what_the_router_speaks('Hola amigo', 'es') == ('Hola amigo', 'es')
