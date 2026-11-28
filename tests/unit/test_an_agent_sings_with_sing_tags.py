"""An agent sings with <sing> tags; a person reads the words, never the tags.

Owner, 28 Nov 2026 ("all 4 parts"; "this shd be whenever singing is needed,
can't we ask the agent to output this regardless of whether agent is created
with this?").  The singing route already existed: Nunba's speech segmenter
sends a reply's <sing>...</sing> words to a singing model.  But no live prompt
told any agent about the tags (the one that did, _get_dynamic_capability_prompt,
is called by nothing), so the rhyme agent spoke its rhyme.  Once agents write
tags, three things must hold, pinned here:

1. Told: a turn the person hears tells the agent how to sing (the draft's
   prompt; the chat and REUSE prompts carry the same SING_TAG_INSTRUCTION).
   A text turn is not told.
2. Read clean: what a person reads (a mid-turn send, a channel reply) has no
   tag, while what is spoken keeps them for the singer.
3. Spoken safely: the speech normaliser's model rewrite sees only the words
   between tags (it could drop or reword a tag), and a speaker that cannot
   sing (the TTS router) says the sung words instead of reading the tags.

Real: words_without_media_tags, normalize_for_tts, publish_agent_message and
turn_sends, ChannelResponseRouter.route_response, the draft prompt builder.
Faked: the realtime publisher (recorded), the normaliser's model (recorded),
the channel sends (recorded).

    python -m pytest tests/unit/test_an_agent_sings_with_sing_tags.py -q
"""
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.channels.media import tts_text_normalizer as norm  # noqa: E402
from integrations.channels.media.tts_text_normalizer import (  # noqa: E402
    SING_TAG_INSTRUCTION, words_without_media_tags)

TURN = '5a1e2c9e-3b0f-4b8e-9d1a-0c7e6f4a2b10'


# 2. What a person reads ------------------------------------------------------

@pytest.mark.parametrize('said, read', [
    ('Here is a rhyme. <sing>Row row row your boat</sing> Did you like it?',
     'Here is a rhyme. Row row row your boat Did you like it?'),
    ('<sing duration="15">Twinkle twinkle little star</sing>',
     'Twinkle twinkle little star'),
    ('<lyrics>Baa baa black sheep</lyrics>', 'Baa baa black sheep'),
    ('Listen<sing>la la</sing>.', 'Listen la la.'),
    ('Relax. <music genre="jazz" duration="10">calm piano</music> Breathe.',
     'Relax. Breathe.'),
    ('<SING>Loud</SING>', 'Loud'),
    ('<sing>a rhyme with no end', 'a rhyme with no end'),
    ('<sing>நிலா நிலா ஓடி வா</sing>', 'நிலா நிலா ஓடி வா'),
    ('first line\n<sing>second line</sing>', 'first line\nsecond line'),
])
def test_a_person_reads_the_words_and_never_a_tag(said, read):
    assert words_without_media_tags(said) == read


@pytest.mark.parametrize('text', [
    'plain text stays', 'a < b and b > c', '', None, '<b>bold</b> is not ours',
    '  spaced  as  written  '])
def test_text_with_no_media_tag_comes_back_exactly_as_it_came(text):
    assert words_without_media_tags(text) is text


# 3. What is spoken -----------------------------------------------------------

@pytest.fixture
def model_rewrite(monkeypatch):
    """The normaliser's model: records what it was given, answers in capitals."""
    seen = []

    def rewrite(text, lang, budget=None):
        seen.append(text)
        return text.upper()

    monkeypatch.setattr(norm, '_llm_normalize', rewrite)
    monkeypatch.setattr(norm, '_cache_get', lambda *a: None)
    monkeypatch.setattr(norm, '_cache_put', lambda *a: None)
    return seen


def test_the_model_rewrite_sees_only_the_words_and_every_tag_is_kept(
        model_rewrite, monkeypatch):
    # every run of words is one the rules could not finish
    monkeypatch.setattr(norm, '_has_residual_tokens',
                        lambda t: any(ch.isalpha() for ch in t))
    out = norm.normalize_for_tts(
        'Count the ships. <sing>One two three</sing> Bye', 'en', use_llm=True)
    assert len(model_rewrite) == 3, model_rewrite
    assert all('<' not in t and '>' not in t for t in model_rewrite), model_rewrite
    assert '<sing>ONE TWO THREE</sing>' in out, out
    # the white space around a rewritten run is kept: no word glued to a tag
    assert 'SHIPS. <sing>' in out and '</sing> BYE' in out, out


def test_a_digit_inside_a_tag_alone_sends_nothing_to_the_model(model_rewrite):
    out = norm.normalize_for_tts(
        '<sing duration="15">la la la</sing>', 'en', use_llm=True)
    assert model_rewrite == []
    assert out.startswith('<sing') and out.endswith('</sing>')


def test_a_line_with_no_tag_still_goes_to_the_model_whole(model_rewrite, monkeypatch):
    monkeypatch.setattr(norm, '_has_residual_tokens', lambda t: True)
    out = norm.normalize_for_tts('Meet me at the gate', 'en', use_llm=True)
    assert len(model_rewrite) == 1, model_rewrite
    assert out == model_rewrite[0].upper()


class _Clock:
    """time.time for the normaliser: moves only when the model is asked."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


class _Bus:
    """The model bus: records each request's options; each answer takes
    ``takes`` seconds on the clock."""

    def __init__(self, clock, takes=0.0):
        self.clock, self.takes, self.calls = clock, takes, []

    def infer(self, model_type, prompt, options):
        self.calls.append(dict(options))
        self.clock.now += self.takes
        return {'response': 'SAID'}


def test_the_clean_up_keeps_one_bus(monkeypatch):
    """hevolve-66e6's 24 h log audit K8 and review D2 (28 Nov): a new bus was
    built for every clean-up, and a new bus has no backends, so each call
    re-ran discovery's health probes before the model was asked; a voice
    reply waited up to 16.6 s.  One bus is built and kept."""
    from integrations.agent_engine import model_bus_service as mbs
    built = []

    class FakeBus:
        def __init__(self):
            built.append(self)

    monkeypatch.setattr(norm, '_BUS', None)
    monkeypatch.setattr(mbs, 'ModelBusService', FakeBus)
    first = norm._get_model_bus()
    assert norm._get_model_bus() is first and len(built) == 1


def test_the_budget_is_not_handed_to_the_bus_as_a_timeout(monkeypatch):
    """review D3: a leg the bus sees time out is marked dead in its class-wide
    health cache for 15-300 s; one slow clean-up would send every other
    caller off the local model."""
    clock = _Clock()
    bus = _Bus(clock)
    monkeypatch.setattr(norm, 'time', clock)
    monkeypatch.setattr(norm, '_get_model_bus', lambda: bus)
    assert norm._llm_normalize('3 apples', 'en') == 'SAID'
    assert 'timeout' not in bus.calls[0]


def test_the_runs_of_one_line_share_one_budget(monkeypatch):
    """Split around its tags, a line never asks the model after its one
    budget is spent: the second run's answer came too late and is not used,
    the third run is not asked."""
    clock = _Clock()
    bus = _Bus(clock, takes=1.5)
    monkeypatch.setattr(norm, 'time', clock)
    monkeypatch.setattr(norm, '_get_model_bus', lambda: bus)
    monkeypatch.setattr(norm, '_cache_get', lambda *a: None)
    monkeypatch.setattr(norm, '_cache_put', lambda *a: None)
    monkeypatch.setattr(norm, '_has_residual_tokens',
                        lambda t: any(ch.isalpha() for ch in t))
    out = norm.normalize_for_tts('one <sing>two</sing> three', 'en', use_llm=True)
    assert len(bus.calls) == 2, bus.calls
    assert out == 'SAID <sing>two</sing> three', out


# 2. Mid-turn sends -----------------------------------------------------------

class TestASendIsReadWithoutItsTags:

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

    def test_the_shown_text_has_no_tag_and_the_turns_record_keeps_them(self):
        said = 'Here it is! <sing>Twinkle twinkle little star</sing>'
        assert self.cp.publish_agent_message(
            text=said, user_id='u1', prompt_id=7,
            request_id=f'{TURN}-intermediate-1')
        shown = [p for p in self.published if isinstance(p, dict) and 'text' in p]
        assert shown and shown[-1]['text'] == [
            'Here it is! Twinkle twinkle little star']
        assert said in self.cp.turn_sends(TURN), (
            'the record the reply is spoken from keeps the tags for the singer')


# 2. Channel replies ----------------------------------------------------------

def test_a_channel_reply_is_read_without_its_tags():
    from integrations.channels.response.router import ChannelResponseRouter
    router = ChannelResponseRouter()
    logged, wamp = [], []
    with patch.object(ChannelResponseRouter, '_log_conversation',
                      lambda self, *a, **k: logged.append(a)), \
            patch.object(ChannelResponseRouter, '_async_fan_out',
                         lambda self, *a, **k: None), \
            patch.object(ChannelResponseRouter, 'deliver_to_chat',
                         lambda self, *a, **k: None), \
            patch('core.safe_hartos_attr.safe_hartos_attr',
                  lambda name: (lambda topic, payload, **f: wamp.append(payload))
                  if name == 'publish_async' else None):
        router.route_response(
            'user1', 'A song! <sing>la la la</sing>',
            {'channel': 'telegram', 'chat_id': '1'}, fan_out=False)
    flat = repr(logged) + repr(wamp)
    assert '<sing>' not in flat and '</sing>' not in flat, flat
    assert 'la la la' in flat


class _ChannelAdapter:
    """A channel adapter that records what it sends to the person's chat."""
    name = 'stub'

    def __init__(self):
        self.sent = []

    def on_message(self, cb):
        pass

    def get_status(self):
        from integrations.channels.base import ChannelStatus
        return ChannelStatus.CONNECTED

    def is_running(self):
        return True

    async def send_typing(self, chat_id):
        pass

    async def send_message(self, chat_id, text, reply_to=None, media=None, buttons=None):
        from integrations.channels.base import SendResult
        self.sent.append(text)
        return SendResult(success=True)


def test_the_reply_to_the_persons_own_chat_has_no_tag():
    """Design review 1, 2c (28 Nov): the reply that goes back to the chat the
    person wrote from (WhatsApp, Telegram ...) is sent by the registry itself,
    not by route_response."""
    import asyncio
    from integrations.channels.base import Message
    from integrations.channels.registry import ChannelRegistry
    registry = ChannelRegistry()
    adapter = _ChannelAdapter()
    registry._adapters['stub'] = adapter
    registry.set_agent_handler(lambda m: 'Here you go! <sing>la la la</sing>')
    asyncio.run(registry._route_to_agent(Message(
        id='m1', channel='stub', sender_id='u', sender_name='U', chat_id='c1',
        text='sing')))
    assert adapter.sent == ['Here you go! la la la']


def test_an_experts_reply_is_read_without_its_tags(dispatcher):
    """Design review 1, 2b: the expert's reply is published for the chat as
    words; it is still handed to the speaker with its tags."""
    published, spoken = [], []

    def attr(name):
        if name == 'publish_async':
            return lambda topic, payload, **k: published.append(payload)
        if name == '_tts_synthesize_and_publish':
            return lambda text, *a, **k: spoken.append(text)
        return None

    with patch('core.safe_hartos_attr.safe_hartos_attr', attr):
        dispatcher._deliver_expert_response(
            'u1', 'p1', 'spec-1', 'Listen. <sing>Twinkle twinkle</sing>')
    assert published and published[0] == 'Listen. Twinkle twinkle', published
    assert all('<sing>' in s for s in spoken), spoken


@pytest.mark.parametrize('said, heard', [
    ('<sing>a rhyme with no end', 'a rhyme with no end'),
    ('one </sing> two', 'one two'),
    ('<sing>a</sing> then <sing>b', 'a then b'),
])
def test_a_tag_left_open_is_not_read_out_and_its_words_are_spoken(said, heard):
    """Design review 1, item 4: the speech segmenter takes only whole pairs
    and would read a lone tag aloud; the chat already shows the words."""
    assert norm.normalize_for_tts(said, 'en', use_llm=False) == heard


def test_whole_pairs_still_reach_the_singer():
    out = norm.normalize_for_tts('Hi <sing>la la</sing> bye', 'en', use_llm=False)
    assert '<sing>la la</sing>' in out


# 1. Told ---------------------------------------------------------------------

@pytest.fixture
def dispatcher():
    from integrations.agent_engine.model_registry import (
        ModelBackend, ModelRegistry, ModelTier)
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    reg = ModelRegistry()
    reg.register(ModelBackend(
        model_id='draft', display_name='Draft', tier=ModelTier.DRAFT,
        config_list_entry={'model': 'd', 'api_key': 'x',
                           'base_url': 'http://localhost:1/v1',
                           'price': [0, 0]},
        avg_latency_ms=300.0, accuracy_score=0.45, is_local=True))
    d = SpeculativeDispatcher(model_registry=reg)
    d._health_probe_enabled = False
    return d


@pytest.fixture
def turn_voice():
    from hartos.threadlocal import thread_local_data
    before = thread_local_data.get_turn_voice()
    yield thread_local_data.set_turn_voice
    thread_local_data.set_turn_voice(before)


def test_a_heard_turn_tells_the_draft_how_to_sing(dispatcher, turn_voice):
    turn_voice({'turn': TURN, 'avatar_id': None, 'media_mode': 'audio',
                'language': 'en'})
    assert SING_TAG_INSTRUCTION in dispatcher._build_draft_classifier_prompt(
        'sing me a rhyme')


def test_a_text_turn_is_not_told(dispatcher, turn_voice):
    turn_voice(None)
    assert '<sing>' not in dispatcher._build_draft_classifier_prompt(
        'sing me a rhyme')


def test_the_instruction_asks_for_no_attribute_and_no_quote():
    """Quote marks are cut from a chat reply before speech and the draft
    answers in one-line JSON: an attribute would come out broken."""
    assert '<sing>' in SING_TAG_INSTRUCTION and '</sing>' in SING_TAG_INSTRUCTION
    assert '"' not in SING_TAG_INSTRUCTION and '=' not in SING_TAG_INSTRUCTION
