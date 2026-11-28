"""A part marked with its language: a person reads the words, never the mark.

Speech design Part A (D:\\claude_scratch\\66e67cad\\speech_out_1128\\design.md,
review 1 MUST 1, 2 and 6; owner 28 Nov 2026: the model tags a language the
way it tags singing).  The model will mark a part written in another language
as <lang es>...</lang>.  One form only, under a name that is ours: a bare
<es>, <tr>, <id>, <it> or <my-app> is ordinary text (review of 88446fbd3,
where the bare form ate a web page table, a path placeholder and a C++ type).
This file pins the reading half, which ships before any model is told about
the marks:

1. Read clean: the one function that takes <sing> out of every text a person
   reads (words_without_media_tags) takes every <lang ...> and </lang> out
   too, whatever is inside, words kept.  What is remembered of a chat reply
   has no mark either.  Any other tag is left as written.
2. Spoken whole: the speech normaliser hands every mark on, untouched, so the
   Nunba segmenter can speak each part in its language.  A sung part left
   open loses its sing tag and keeps its language mark.
3. One grammar: tests/fixtures/speech_tags_cases.json is the same file in
   Nunba-HART-Companion, where the segmenter is run against it; its sha256 is
   pinned in both repos.
4. The recorded-voice path (TTSRouter.synthesize: one engine, no segmenter)
   speaks a reply marked whole in the marked language, by its base code, and
   never a mark.

Real: words_without_media_tags, language_marked_on_every_word,
normalize_for_tts, TTSRouter.synthesize, publish_agent_message, _chat_reply.
Faked: the engine choice (recorded), the realtime publisher (recorded), the
normaliser's cache (off), the chat memory and the memory graph (recorded),
langchain_classic's message module (not installed in this venv; an empty
stand-in so the memory save is reached).

    python -m pytest tests/unit/test_a_part_marked_with_its_language_keeps_its_words_and_loses_its_mark.py -q
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import types
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from integrations.channels.media import tts_text_normalizer as norm  # noqa: E402
from integrations.channels.media.tts_text_normalizer import (  # noqa: E402
    language_marked_on_every_word, words_without_media_tags)

FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'speech_tags_cases.json')
# The same constant is in Nunba-HART-Companion
# tests/test_a_part_marked_with_its_language_is_spoken_in_it.py.
FIXTURE_SHA256 = '0f227d3144f54754a33a1de835a75e839ec9f9cc8f8b21cb6e7b0efed8dafe49'

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


# 1. What a person reads ------------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=_IDS)
def test_a_person_reads_the_words_and_never_a_language_mark(case):
    assert words_without_media_tags(case['input']) == case['shown']


@pytest.mark.parametrize('said, read', [
    # whatever is inside the brackets, the mark is not shown
    ('<lang es-419>Hola</lang>', 'Hola'),
    ('<lang español>Hola</lang>', 'Hola'),
    ('<lang cs>Ahoj</lang>', 'Ahoj'),
    ('<lang>Hola</lang>', 'Hola'),
    ('<lang code="es" x>Hola</lang >', 'Hola'),
    ('<LANG ES>Hola</LANG>', 'Hola'),
])
def test_a_lang_tag_is_never_shown_whatever_is_inside_it(said, read):
    assert words_without_media_tags(said) == read


@pytest.mark.parametrize('text', [
    # measured on 88446fbd3 by its reviewer: each of these lost a tag
    '<table><tr><th>Name</th></tr><tr><td>Zoey</td></tr></table>',
    '/users/<id>', 'use the <it> tag', 'vector<it> x', '<my-app></my-app>',
    '<en-dash>', '<or-else>', '<id-num>', '<es>Hola</es>', '<pt-BR>Olá</pt-BR>',
    '<tr>Merhaba</tr>', '<th>x</th>',
    '<b>bold</b> is not ours', 'line one<br>line two', '<i>x</i> <p>y</p>',
    '<a>link</a> and <em>stress</em>', '<div>box</div> <pre>code</pre>',
    'git push origin <sha>:main', 'a < lang es > b', '<language>', '<langs>',
    'if a <b and c> d'])
def test_every_other_tag_comes_back_exactly_as_it_came(text):
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
        said = 'In Spanish: <lang es>Hola amigo</lang>'
        assert self.cp.publish_agent_message(
            text=said, user_id='u1', prompt_id=7,
            request_id=f'{turn}-intermediate-1')
        shown = [p for p in self.published if isinstance(p, dict) and 'text' in p]
        assert shown and shown[-1]['text'] == ['In Spanish: Hola amigo']
        assert said in self.cp.turn_sends(turn), (
            'the record the reply is spoken from keeps the mark for the speaker')


def test_what_is_remembered_of_a_chat_reply_has_no_mark(monkeypatch):
    """Review of 88446fbd3: the memory save and the graph register ran before
    the marks were taken out, so a remembered mark came back in a later
    prompt.  The speaker still gets the marks."""
    monkeypatch.setenv('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())
    import hart_intelligence_entry as hie
    spoken, saved, graphed = [], [], []
    registered = threading.Event()

    class _Memory:
        def save_context(self, said, answered):
            saved.append(answered['output'])

    class _Graph:
        def register_conversation(self, who, text, key):
            graphed.append((who, text))
            if who == 'langchain':
                registered.set()

    messages = types.ModuleType('langchain_classic.schema.messages')
    messages.HumanMessage = messages.AIMessage = object
    for name in ('langchain_classic', 'langchain_classic.schema'):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    if 'langchain_classic.schema.messages' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'langchain_classic.schema.messages',
                            messages)
    monkeypatch.setattr(hie, '_tts_synthesize_and_publish',
                        lambda text, *a, **k: spoken.append(text))
    monkeypatch.setattr(hie, 'get_memory', lambda user_id: _Memory())
    monkeypatch.setattr(hie, '_get_or_create_graph', lambda *a, **k: _Graph())
    monkeypatch.setattr('core.safe_hartos_attr.safe_hartos_attr', lambda name: None)

    said = 'In Spanish: <lang es>Hola amigo</lang> <sing>la la</sing>'
    with hie.app.test_request_context(
            '/chat', json={'media_mode': 'audio', 'prompt': 'say hello'}):
        body = hie._chat_reply('u1', '3d9a7c5e-1b2f-4a6c-9e8d-7f0a1b2c3d4e', said,
                               user_prompt='say hello in Spanish').get_json()
    assert registered.wait(10), 'the graph register never ran'
    assert saved == ['In Spanish: Hola amigo la la']
    assert ('langchain', 'In Spanish: Hola amigo la la') in graphed
    assert body['response'] == 'In Spanish: Hola amigo la la'
    assert spoken and '<lang es>Hola amigo</lang>' in spoken[-1]


# 2. What is spoken -----------------------------------------------------------

@pytest.mark.parametrize('said, source, handed_on', [
    ('Hi. <lang es>Hola amigo</lang> Bye.', None,
     'Hi. <lang es>Hola amigo</lang> Bye.'),
    # a chat reply's quote marks are cut before speech; the mark still reads
    ('<lang="es">Hola</lang>', 'chat_response', '<lang=es>Hola</lang>'),
    ('<lang es><sing>la la</sing></lang>', None,
     '<lang es><sing>la la</sing></lang>'),
    ('<sing><lang es>la la</lang></sing>', None,
     '<sing><lang es>la la</lang></sing>'),
    # the words inside a marked part are still made sayable
    ('It costs <lang es>$5 hoy</lang>', None,
     'It costs <lang es>5 dollars hoy</lang>'),
    # open and stray marks go on as written: the segmenter closes or drops them
    ('Now: <lang ta>vanakkam nanba', None, 'Now: <lang ta>vanakkam nanba'),
    ('Hello </lang> world', None, 'Hello </lang> world'),
    ('<lang pt-BR>Bom dia</lang>', None, '<lang pt-BR>Bom dia</lang>'),
    # no mark, nothing changed
    ('GET /users/<id> now', None, 'GET /users/<id> now'),
    ('Say <es>Hola</es>', None, 'Say <es>Hola</es>'),
])
def test_the_speaker_is_handed_every_mark_as_written(said, source, handed_on,
                                                     no_cache):
    assert norm.normalize_for_tts(said, 'en', use_llm=False,
                                  source=source) == handed_on


def test_a_sung_part_left_open_keeps_its_language_mark(no_cache):
    """The sing tag would be read aloud, so it goes; the language mark is
    read by the segmenter open or closed, so it stays."""
    assert norm.normalize_for_tts(
        '<sing>la la <lang es>hola amigo</lang>', 'en', use_llm=False) == (
        'la la <lang es>hola amigo</lang>')
    assert norm.normalize_for_tts(
        '<lang es>hola <sing>la la', 'en', use_llm=False) == (
        '<lang es>hola la la')


def test_a_language_mark_left_open_is_not_a_broken_sung_part(no_cache):
    assert norm.normalize_for_tts(
        '<lang ta>vanakkam <sing>nila nila</sing>', 'en', use_llm=False) == (
        '<lang ta>vanakkam <sing>nila nila</sing>')


def test_the_model_rewrite_never_sees_a_language_mark(monkeypatch, no_cache):
    seen = []
    monkeypatch.setattr(norm, '_llm_normalize',
                        lambda text, lang, budget=None: seen.append(text) or text.upper())
    monkeypatch.setattr(norm, '_has_residual_tokens',
                        lambda t: any(ch.isalpha() for ch in t))
    out = norm.normalize_for_tts(
        'Say it. <lang es>Hola amigo</lang> Bye', 'en', use_llm=True)
    assert len(seen) == 3 and all('<' not in t and '>' not in t for t in seen), seen
    assert out == 'SAY IT. <lang es>HOLA AMIGO</lang> BYE', out


# 4. One engine, no segmenter -------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=_IDS)
def test_the_one_language_every_word_is_marked_with(case):
    assert language_marked_on_every_word(case['input']) == case['whole_language']


@pytest.mark.parametrize('text', [
    'plain words', '', None, 'a < b', '<es>Hola</es>', '<tr>Merhaba</tr>',
    '/users/<id>'])
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
        '<lang es>Hola amigo, vamos al parque</lang>', 'en') == (
        'Hola amigo, vamos al parque', 'es')


def test_the_recorded_voice_is_given_the_base_code_of_a_regional_mark():
    assert _what_the_router_speaks('<lang zh-Hans>我们去公园</lang>', 'en') == (
        '我们去公园', 'zh')


def test_the_recorded_voice_speaks_a_sung_spanish_reply_in_spanish():
    assert _what_the_router_speaks(
        '<sing><lang es>la cucaracha</lang></sing>', 'en') == (
        'la cucaracha', 'es')


def test_the_recorded_voice_never_says_a_mark_of_a_reply_marked_in_parts():
    """One engine says the whole text: it keeps the language asked for, and
    the marks are gone."""
    assert _what_the_router_speaks(
        'In Spanish: <lang es>Hola amigo</lang>', 'en') == (
        'In Spanish: Hola amigo', 'en')


def test_the_recorded_voice_says_a_bare_code_in_brackets_as_written():
    assert _what_the_router_speaks('Say <es>Hola</es>', 'en') == (
        'Say <es>Hola</es>', 'en')


def test_the_recorded_voice_keeps_the_language_asked_for_with_no_mark():
    assert _what_the_router_speaks('Hola amigo', 'es') == ('Hola amigo', 'es')
