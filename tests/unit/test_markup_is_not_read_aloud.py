"""Markup and emoji are written for the eye; no speaker reads them aloud.

The defect (task #184): "**Well done**" was spoken as "asterisk asterisk
well done asterisk asterisk" by every speaker except the chat reply, because
the cut lived as an inline copy on the chat path
(hart_intelligence_entry._tts_synthesize_and_publish) and a smaller one in
Nunba, while the one normalizer every speaker calls, normalize_for_tts, cut
nothing.

The cut is now the normalizer's (strip_for_speech).  These tests hold:
  - every source loses markup and emoji;
  - a chat reply is cut exactly as the inline copy cut it (rows recorded
    from that copy at 1fef56008 before it was deleted);
  - any other source gets text with no markup back unchanged, keeps its
    links for the rule pass to announce, and keeps its line breaks;
  - there only a PAIR of marks around words is emphasis: "5*4", "2**3",
    "b_ll", "snake_case" and "~5 km" are said as written (review rv_66ba of
    66ba446bb, which cut every mark and gave a child "54" for "5*4");
  - a media tag's attributes survive the number rule;
  - nobody keeps a second copy of the cut.
"""
import os
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from integrations.channels.media import tts_text_normalizer as tn  # noqa: E402

CHAT = 'chat_response'

# (reply as written, what the chat path's inline cut made of it at 1fef56008)
CHAT_ROWS = [
    ('Result ```print(1)``` and **bold** see https://x.com \U0001F642 done',
     'Result and bold see done'),
    ('# Title\n\nSome *emphasis* and __strong__ text.',
     'Title Some emphasis and strong text.'),
    ('We\'ll go, she said "now" [laughs] ok', 'Well go, she said now ok'),
    ('Read [the guide](https://docs.example.com/a_b) first',
     'Read the guide first'),
    ('C# is a language and issue #5 is open',
     'Cis a language and issue #5 is open'),
    ('family \U0001F468‍\U0001F469‍\U0001F467 and sun ☀️ here',
     'family and sun here'),
    ('ക്‍ഷ in Malayalam', 'ക്ഷ in Malayalam'),
    ('mail john_doe@mail.com or see http://a.b/c',
     'mail johndoe@mail.com or see'),
    ("<music genre='jazz' duration='10'>chill</music> hi",
     '<music genre=jazz duration=10>chill</music> hi'),
    ('``a`b` and ~~gone~~ and a*b*c', '`b` and gone and abc'),
    ('\U0001F642', ''),
    ('   ', ''),
    ('plain text stays', 'plain text stays'),
]

# Text with no markup: a source other than a chat reply gets it back as is.
UNMARKED = [
    'plain text stays',
    'We\'ll go, she said "now" [laughs] ok',
    'C# is a language and issue #5 is open',
    'ക്‍ഷ in Malayalam',
    'mail john_doe@mail.com today',
    'see https://ex.com/p_q?x=*1 now',
    'two  spaces\nand a line\n\nbreak\t.',
    '  padded  ',
    'a < 5 and b > 3',
    # a lone mark is part of what is said (rv_66ba)
    'What is 5*4? It is 20. Fill the blank: b_ll',
    '2*3=6',
    '2**3 is 8',
    '2*3*4=24 and x = a*b + c*d',
    'a * b = c',
    'snake_case_name and file_name.py',
    'edit __init__.py',
    'about ~5 km away, or ~6 km',
    'Stretch it: sssss-nake, s~s~s',
    '- item one\n* item two\n1. first',
    '** x **',
    '*not closed',
    '**bold**text',
    # a mark that follows a word character opens nothing
    'x*y* and a_b_ end',
    # a mark that is followed by another mark opens nothing
    '** x* and __ y_',
]


class _Cache(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir
        self._llm = patch.object(tn, '_llm_normalize', lambda *a, **k: None)
        self._llm.start()

    def tearDown(self):
        self._llm.stop()
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev


class EverySpeakerLosesMarkup(_Cache):

    def test_emphasis_marks_are_not_said(self):
        self.assertEqual(tn.normalize_for_tts('**Well done**, *you* did ~~it~~'),
                         'Well done, you did it')

    def test_emoji_is_not_said(self):
        got = tn.normalize_for_tts('Great \U0001F642 job \U0001F468‍\U0001F469‍\U0001F467')
        self.assertEqual(got, 'Great job')

    def test_code_is_not_said(self):
        got = tn.normalize_for_tts('Run ```pip install x``` or `ls` now')
        self.assertEqual(got, 'Run or now')

    def test_a_named_link_is_said_by_its_name(self):
        got = tn.normalize_for_tts('Read [the guide](https://docs.example.com/a)')
        self.assertEqual(got, 'Read the guide')

    def test_a_header_mark_is_not_said(self):
        got = tn.normalize_for_tts('## Steps\nfirst this')
        self.assertEqual(got, 'Steps\nfirst this')

    def test_only_markup_leaves_nothing_to_say(self):
        self.assertEqual(tn.normalize_for_tts('**\U0001F642**').strip(), '')

    def test_a_pair_inside_a_pair_goes_too(self):
        self.assertEqual(tn.strip_for_speech('**a *b* c** and ***d***'),
                         'a b c and d')

    def test_emphasis_in_other_scripts(self):
        self.assertEqual(
            tn.strip_for_speech('\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd '
                                '**\u0ba8\u0ba3\u0bcd\u0baa\u0bbe**!'),
            '\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd '
            '\u0ba8\u0ba3\u0bcd\u0baa\u0bbe!')
        self.assertEqual(tn.strip_for_speech('\u0928\u092e\u0938\u094d\u0924\u0947 '
                                             '*\u0926\u094b\u0938\u094d\u0924*,'),
                         '\u0928\u092e\u0938\u094d\u0924\u0947 '
                         '\u0926\u094b\u0938\u094d\u0924,')

    def test_a_keycap_digit_is_said_as_the_digit(self):
        self.assertEqual(tn.strip_for_speech('3\ufe0f\u20e3 steps'), '3 steps')

    def test_a_marked_price_and_letter_are_said(self):
        self.assertEqual(tn.strip_for_speech('Price: *$100*, the letter _b_'),
                         'Price: $100, the letter b')

    def test_the_same_words_are_cached_once(self):
        """The cache is keyed by what is said, not by how it was marked up."""
        tn.normalize_for_tts('**Hello** there')
        tn.normalize_for_tts('Hello *there*')
        self.assertEqual(len(os.listdir(self._tmpdir)), 1)


class AChatReplyIsCutAsItAlwaysWas(_Cache):

    def test_recorded_rows(self):
        for text, was in CHAT_ROWS:
            with self.subTest(text=text):
                self.assertEqual(tn.strip_for_speech(text, CHAT), was)

    def test_a_bare_link_is_dropped_not_announced(self):
        got = tn.normalize_for_tts('see https://x.com now', 'en', source=CHAT)
        self.assertEqual(got, 'see now')

    def test_the_chat_path_speaks_the_owners_cut(self):
        """_tts_synthesize_and_publish hands the engine what the owner made
        of the reply: the reply is cut once, by normalize_for_tts."""
        import hart_intelligence_entry as hie

        captured = {}

        def _synthesize_text(t, language='en', **kw):
            captured['text'] = t
            return ''

        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine_mod.synthesize_text = _synthesize_text
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod

        class _Inline:
            def submit(self, fn, *a, **k):
                fn(*a, **k)

        seen = []
        voiced = []
        real = tn.normalize_for_tts

        def _spy(text, lang='en', use_llm=True, source=None):
            seen.append((text, source))
            return real(text, lang, use_llm=False, source=source)

        reply = 'We\'ll say **this** [aside] and ``a`b` ok'
        with patch.dict(sys.modules,
                        {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _Inline()), \
                patch.object(hie, '_speak_in_voice',
                             lambda *a: voiced.append(a)), \
                patch.object(tn, 'normalize_for_tts', _spy):
            hie._tts_synthesize_and_publish(reply, 'user-1', 'req-1',
                                            language='en')
        # the voiced branch (the router cuts for itself) gets the reply as
        # written, and so does the default voice's normalizer call
        self.assertEqual(voiced, [(reply, 'en', None)])
        self.assertEqual(seen, [(reply, CHAT)])
        self.assertEqual(captured['text'], 'Well say this and `b` ok')

    def test_a_reply_that_is_only_markup_is_not_spoken(self):
        import hart_intelligence_entry as hie

        called = []
        engine_mod = types.ModuleType('tts.tts_engine')
        engine_mod.get_tts_engine = lambda: types.SimpleNamespace(
            backend_name='fake-engine')
        engine_mod.synthesize_text = lambda *a, **k: called.append(a) or ''
        pkg = types.ModuleType('tts')
        pkg.tts_engine = engine_mod

        class _Inline:
            def submit(self, fn, *a, **k):
                fn(*a, **k)

        with patch.dict(sys.modules,
                        {'tts': pkg, 'tts.tts_engine': engine_mod}), \
                patch.object(hie, '_tts_executor', _Inline()), \
                patch.object(hie, '_speak_in_voice',
                             lambda *a: called.append(a)):
            hie._tts_synthesize_and_publish('** \U0001F642 ```x``` **',
                                            'user-1', 'req-1', language='en')
        self.assertEqual(called, [])


class AnyOtherSourceKeepsWhatIsNotMarkup(_Cache):

    def test_text_with_no_markup_comes_back_unchanged(self):
        for text in UNMARKED:
            with self.subTest(text=text):
                self.assertEqual(tn.strip_for_speech(text), text)
                self.assertEqual(tn.strip_for_speech(text, 'read_aloud'), text)

    def test_a_bare_link_is_still_announced(self):
        got = tn.normalize_for_tts('**see** https://x.com/a_b now', 'en')
        self.assertEqual(got, 'see link now')

    def test_an_address_keeps_its_underscore(self):
        got = tn.normalize_for_tts('*mail* john_doe@mail.com', 'en')
        self.assertEqual(got, 'mail john_doe at mail dot com')

    def test_quotes_brackets_and_a_sharp_stay(self):
        got = tn.strip_for_speech('*Yes*: we\'ll use C# is [fine]')
        self.assertEqual(got, 'Yes: we\'ll use C# is [fine]')

    def test_a_joiner_inside_a_word_stays(self):
        word = 'ക്‍ഷ'
        self.assertEqual(tn.strip_for_speech(f'*{word}* \U0001F642'), word)

    def test_line_breaks_stay_when_something_was_cut(self):
        got = tn.strip_for_speech('# One\n\n**two**  three\nfour')
        self.assertEqual(got, 'One\n\ntwo three\nfour')

    def test_the_router_hands_its_source_to_the_normalizer(self):
        from integrations.channels.media import tts_router
        seen = []

        def _normalize(text, lang, use_llm=True, source=None):
            seen.append(source)
            return text

        router = tts_router.TTSRouter()
        with patch.object(tn, 'normalize_for_tts', side_effect=_normalize), \
                patch.object(router, 'select_engines', return_value=[]):
            router.synthesize('one **two**', language='en', source=CHAT)
            router.synthesize('one **two**', language='en', source='call')
            router.synthesize('one **two**', language='en')
        self.assertEqual(seen, [CHAT, 'call', None])

    def test_the_router_hands_the_engine_a_sum_as_written(self):
        """The probe of rv_66ba: every source but the chat reply."""
        from integrations.channels.media import tts_router
        router = tts_router.TTSRouter()
        said = 'What is 5*4? It is 20. Fill the blank: b_ll'
        for source in ('call', 'agent_tool', 'read_aloud', None):
            spoken = []
            with self.subTest(source=source), \
                    patch.object(tn, '_num_to_words', lambda n, lang: None), \
                    patch.object(router, 'select_engines',
                                 side_effect=lambda t, *a, **k:
                                 spoken.append(t) or []):
                router.synthesize(said, language='en', source=source)
                self.assertEqual(spoken, [said])

    def test_the_router_speaks_no_markup(self):
        from integrations.channels.media import tts_router
        router = tts_router.TTSRouter()
        spoken = []

        def _select(text, *a, **k):
            spoken.append(text)
            return []

        with patch.object(router, 'select_engines', side_effect=_select):
            router.synthesize('**Well done** \U0001F642', language='en',
                              source='call')
        if spoken:
            self.assertEqual(spoken, ['Well done'])
        else:
            self.fail('the router never reached engine selection')


class AMediaTagIsPassedThrough(_Cache):

    def test_its_attributes_are_not_spelled_out(self):
        # fade='50%' would become 'percent' on any box; duration='10' would
        # become 'ten' where num2words is installed
        tag = "<music genre='jazz' duration='10' fade='50%'>"
        got = tn.normalize_for_tts(f"hi {tag}chill</music> bye", 'en')
        self.assertIn(tag, got)
        self.assertIn('</music>', got)

    def test_the_words_around_it_still_are(self):
        got = tn.rule_normalize('pay Rs.200 <sing duration="15">la</sing> 12%',
                                'en')
        self.assertIn('<sing duration="15">', got)
        self.assertIn('rupees', got)
        self.assertIn('percent', got)
        self.assertNotIn('12%', got)


class NobodyKeepsASecondCopyOfTheCut(unittest.TestCase):
    """Shape, not names: a regex that deletes emphasis marks, code fences or
    emoji ranges in a module that is not the normalizer is a second copy."""

    OWNER = 'integrations/channels/media/tts_text_normalizer.py'
    # The chat path was the copy; it must stay gone.
    WATCHED = ['hart_intelligence_entry.py',
               'integrations/channels/media/tts_router.py',
               'integrations/channels/media/tts.py']
    SHAPES = [r'\[\*_~\]', r'```\[\\s\\S\]\*\?```', r'U0001F600-']

    def test_no_copy_on_the_speaking_paths(self):
        found = []
        for rel in self.WATCHED:
            src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
            for shape in self.SHAPES:
                if re.search(shape, src):
                    found.append((rel, shape))
        self.assertEqual(found, [])

    def test_the_owner_has_them(self):
        src = open(os.path.join(ROOT, self.OWNER), encoding='utf-8').read()
        for shape in self.SHAPES:
            self.assertTrue(re.search(shape, src), shape)


if __name__ == '__main__':
    unittest.main()
