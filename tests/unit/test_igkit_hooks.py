"""integrations.reel_studio.igkit: shared text primitives, the hook checker
and the beat sheet, ported from instagram-agent-skill (MIT).

Upstream counted Tamil as zero words, timed a Tamil line at 0 s and split
no sentence at the Hindi danda (skill-port report, finding 4).  These tests
pin the port: words in any script, Indian money/number/occasion words as
concrete markers, makeup risk words as stakes, hook verdicts without a
numeric score (it does not predict performance), and the makeup formulas.
"""
import pytest

from integrations.reel_studio import igkit
from integrations.reel_studio.igkit import hooks

TAMIL_HOOK = 'இந்த ப்ரைடல் லுக் பாருங்க'  # "look at this bridal look"
ZWJ = '\u200d'
ZWNJ = '\u200c'


class TestWords:
    def test_tamil_words_stay_whole(self):
        assert igkit.words(TAMIL_HOOK) == ['இந்த', 'ப்ரைடல்', 'லுக்', 'பாருங்க']

    def test_hindi_words_stay_whole(self):
        assert igkit.words('यह दुल्हन का लुक है। बहुत सुंदर॥ 40 मिनट') == [
            'यह', 'दुल्हन', 'का', 'लुक', 'है', 'बहुत', 'सुंदर', '40', 'मिनट']

    def test_the_danda_ends_a_sentence(self):
        assert igkit.sentences('यह दुल्हन का लुक है। बहुत सुंदर॥ 40 मिनट') == [
            'यह दुल्हन का लुक है।', 'बहुत सुंदर॥', '40 मिनट']

    def test_english_sentences_split_as_before(self):
        assert igkit.sentences('One look. Two looks!\nThree?') == [
            'One look.', 'Two looks!', 'Three?']

    def test_a_joiner_stays_inside_an_indic_word(self):
        assert igkit.words('क्' + ZWJ + 'ष') == ['क्' + ZWJ + 'ष']
        assert igkit.words('क्' + ZWNJ + 'ष') == ['क्' + ZWNJ + 'ष']

    def test_the_joiners_are_zwnj_and_zwj(self):
        # Written as escapes in the source: an editor that strips invisible
        # characters would otherwise empty this set without a diff anyone sees.
        assert igkit.JOINERS == frozenset({'\u200c', '\u200d'})

    def test_a_decimal_point_does_not_end_a_sentence(self):
        assert igkit.sentences('Bridal from ₹1.5 lakh. Book 2.5 hours early!') == [
            'Bridal from ₹1.5 lakh.', 'Book 2.5 hours early!']
        assert igkit.sentences('Done at 5. Then photos.') == ['Done at 5.', 'Then photos.']

    def test_money_is_one_word_and_lone_punctuation_is_none(self):
        assert igkit.words('₹1,50,000 - $18,000 —') == ['₹150000', '$18000']

    def test_concrete_markers_cover_indian_money_numbers_occasions_brands(self):
        found = igkit.concrete_markers('இருபது நிமிடத்தில் ₹15k மேக்கப், MAC மற்றும் ஹல்தி')
        assert sorted(found) == sorted(['இருபது', '₹15k', 'MAC', 'ஹல்தி'])

    def test_a_name_starting_a_sentence_is_not_a_marker(self):
        assert igkit.concrete_markers('Simple look. Clean base.') == []
        assert igkit.concrete_markers('Her look for Priya was simple.') == ['Priya']

    def test_a_brand_written_as_a_name_counts_once(self):
        assert igkit.concrete_markers('Base by Kryolan only') == ['Kryolan']


class TestSharedPrimitives:
    """One definition each of a hashtag, an emoji and a yes/no value, for
    the caption linter, the hook check, the humanizer, swipe and her voice
    profile."""

    def test_hashtags_spaced_glued_or_after_an_emoji_in_any_script(self):
        text = '#a #b#c ✨#மணப்பெண் (#d) x#y page#z ## C#'
        assert [t for t, _, _ in igkit.hashtags(text)] == [
            '#a', '#b', '#c', '#மணப்பெண்', '#d']

    def test_hashtag_positions(self):
        assert igkit.hashtags('ab #cd') == [('#cd', 3, 6)]
        assert igkit.hashtags('') == [] and igkit.hashtags(None) == []

    @pytest.mark.parametrize('text,units', [
        ('✨\U0001F484', ['✨', '\U0001F484']),
        ('1\ufe0f\u20e3 \U0001F1EE\U0001F1F3', ['1\ufe0f\u20e3', '\U0001F1EE\U0001F1F3']),
        ('\U0001F470\u200d♀\ufe0f✨', ['\U0001F470\u200d♀\ufe0f', '✨']),
        ('Bare face → bride!!', []),
    ])
    def test_emoji_as_she_sees_them(self, text, units):
        assert igkit.EMOJI_RE.findall(text) == units


class TestRateHook:
    def test_a_tamil_hook_is_counted_not_empty(self):
        r = hooks.rate_hook(TAMIL_HOOK)
        assert r['words'] == 4
        assert r['checks']['LENGTH']['score'] == 80.0     # one word short of 5
        assert r['checks']['FRONTLOAD']['detail'] != 'empty'
        # A verb-final imperative (பாருங்க, "look") speaks to the viewer.
        assert r['checks']['ADDRESS']['score'] == 90.0

    def test_tamil_numbers_money_and_occasions_are_concrete(self):
        r = hooks.rate_hook('இருபது நிமிடத்தில் முகூர்த்தம் லுக், ₹15k மட்டும்')
        assert r['checks']['SPECIFICITY']['score'] == 100.0

    def test_makeup_risk_words_are_stakes(self):
        r = hooks.rate_hook('Dusky skin turning grey in flash photos at the reception')
        assert r['checks']['STAKES']['score'] == 100.0

    def test_an_english_greeting_is_a_dealbreaker(self):
        r = hooks.rate_hook('Hey guys, today I will show you my bridal makeup')
        assert any(f.startswith('Greeting') for f in r['flags'])

    def test_a_tamil_greeting_is_a_dealbreaker(self):
        r = hooks.rate_hook('வணக்கம் friends, இன்னைக்கு ஒரு bridal look')
        assert any(f.startswith('Greeting') for f in r['flags'])

    @pytest.mark.parametrize('hook', ['Bare face to bride in 20 minutes ✨#bridal',
                                      'Bare face to bride in 20 minutes #a#b',
                                      'மணப்பெண் லுக் 20 நிமிடத்தில் #மணப்பெண்'])
    def test_a_hashtag_anywhere_is_one_dealbreaker(self, hook):
        flags = hooks.rate_hook(hook)['flags']
        assert sum(f.startswith('Hashtag') for f in flags) == 1

    def test_a_hash_inside_a_word_is_not_a_hashtag(self):
        flags = hooks.rate_hook('Look#2 in 20 minutes, no grey cast')['flags']
        assert not any(f.startswith('Hashtag') for f in flags)

    def test_a_keycap_in_the_hook_is_an_emoji(self):
        flags = hooks.rate_hook('1\ufe0f\u20e3 base mistake brides make in humidity')['flags']
        assert any(f.startswith('Emoji') for f in flags)

    def test_a_word_that_starts_with_hi_is_not_a_greeting_opener(self):
        # Upstream matched openers as prefixes, so "Highlighter" opened with "hi".
        r = hooks.rate_hook('Highlighter that lasts 12 hours in Chennai humidity')
        assert 'weak opener' not in r['checks']['FRONTLOAD']['detail']
        r = hooks.rate_hook('Hi everyone, highlighter that lasts 12 hours')
        assert 'weak opener "hi"' in r['checks']['FRONTLOAD']['detail']

    def test_a_name_is_the_payload_word(self):
        # Upstream's name pattern could never match a single word (it refused
        # position 0), so a name never counted as the payload.
        r = hooks.rate_hook('My bride Priya waited for this look')
        assert r['checks']['FRONTLOAD']['detail'].startswith('payload at word 3')

    def test_the_weakest_check_is_named(self):
        r = hooks.rate_hook('Bridal makeup look')
        scores = {k: v['score'] for k, v in r['checks'].items()}
        assert r['weakest'] == min(hooks.CHECKS, key=lambda c: scores[c])


class TestScoreHooks:
    GOOD = 'You lose the base after 2 hours of sweat. Try this.'
    WEAK = 'Hey guys, welcome back to my channel'

    def test_verdict_and_weakest_only_no_numeric_score(self):
        out = hooks.score_hooks([self.GOOD, self.WEAK])
        assert 'does not predict' in out['note']
        good, weak = out['hooks']
        assert set(good) == {'hook', 'verdict', 'weakest', 'flags', 'formula_id',
                             'formula', 'card_ok', 'card_words'}
        assert good['verdict'] == 'OK'
        assert weak['verdict'] == 'WEAK'
        assert weak['weakest']['check'] in hooks.CHECKS
        assert weak['weakest']['detail']

    def test_a_dealbreaker_makes_it_weak(self):
        out = hooks.score_hooks(['Stop scrolling: 2 hours of sweat and the base held'])
        assert out['hooks'][0]['verdict'] == 'WEAK'
        assert out['hooks'][0]['flags']

    def test_input_order_kept_and_blank_lines_dropped(self):
        out = hooks.score_hooks([self.WEAK, '  ', self.GOOD])
        assert [h['hook'] for h in out['hooks']] == [self.WEAK, self.GOOD]

    def test_a_newline_separated_string_is_accepted(self):
        out = hooks.score_hooks(self.WEAK + '\n' + self.GOOD)
        assert [h['hook'] for h in out['hooks']] == [self.WEAK, self.GOOD]

    def test_none_is_no_hooks(self):
        assert hooks.score_hooks(None) == {'note': hooks.HOOK_NOTE, 'hooks': []}

    @pytest.mark.parametrize('bad', [42, 4.2, b'Bare face to bride', {'hook': 'Bare face'},
                                     object()])
    def test_anything_but_text_or_lines_is_refused_by_name(self, bad):
        with pytest.raises(TypeError, match=type(bad).__name__):
            hooks.score_hooks(bad)

    def test_card_fit_uses_the_on_screen_word_limit(self):
        out = hooks.score_hooks(['Bare face to bride in 20 minutes', 'Bare face to bride'])
        long, short = out['hooks']
        assert (long['card_words'], long['card_ok']) == (7, False)
        assert (short['card_words'], short['card_ok']) == (4, True)
        assert hooks.CARD_MAX_WORDS == 6


class TestClassify:
    @pytest.mark.parametrize('hook,name', [
        ('Bare face to bride in 20 seconds', 'Bare Face To Bride'),
        ('Wait for the temple jewellery', 'Jewellery Or Drape Reveal'),
        ('இந்த நகையைப் பாருங்க', 'Jewellery Or Drape Reveal'),
        ("My bride's reaction when she saw the mirror", 'Client Reaction'),
        ('Dusky skin, no grey cast in flash photos', 'Shade Match'),
        ('Nobody tells you that your first 30 reels are supposed to flop.',
         'Nobody Tells You'),
        ('$18,000 is what one missing clause cost me.', 'Cost Confession'),
    ])
    def test_formula(self, hook, name):
        assert hooks.classify_hook(hook)[1] == name

    @pytest.mark.parametrize('hook,name', [
        ('Bridal transformation in 20 seconds', 'Bare Face To Bride'),
        ('Wait for the final look', 'Jewellery Or Drape Reveal'),
        ('Gold nath and maang tikka, last shot', 'Jewellery Or Drape Reveal'),
        ('Her reaction when she saw the mirror', 'Client Reaction'),
        ('மணப்பெண்ணின் ரியாக்ஷன்', 'Client Reaction'),
        ('दुल्हन का रिएक्शन देखिए', 'Client Reaction'),
    ])
    def test_makeup_formula(self, hook, name):
        assert hooks.classify_hook(hook)[1] == name

    @pytest.mark.parametrize('hook,not_id', [
        # The makeup formulas are classified first, so a broad pattern in one
        # of them relabels other niches' hooks in the research swipe makes.
        ('The transformation of my business in 2 years', 27),
        ('Wait for the end, you will laugh', 29),
        ('Ranbir Nath on startup funding', 29),
        ('My reaction to the new budget', 30),
        ('बजट पर मेरा रिएक्शन', 30),
    ])
    def test_a_makeup_formula_does_not_take_other_niches_hooks(self, hook, not_id):
        assert hooks.classify_hook(hook)[0] != not_id

    def test_unclassified(self):
        assert hooks.classify_hook('a quiet morning') == (None, 'unclassified')


class TestBeatSheet:
    def test_tamil_lines_are_timed_by_their_words(self):
        script = TAMIL_HOOK + '\nமுகூர்த்தம் காலை 5 மணிக்கு.\nஎன்னை DM பண்ணுங்க'
        a = hooks.beat_sheet(script, wpm=120)
        assert [b['words'] for b in a['beats']] == [4, 4, 3]
        assert a['total_words'] == 11
        assert a['total_seconds'] == pytest.approx(5.5, abs=0.01)
        assert a['beats'][0]['label'] == 'HOOK'
        assert a['beats'][-1]['label'] == 'CTA'
        assert a['beats'][1]['concrete'] >= 2          # முகூர்த்தம் and 5

    def test_a_long_hindi_line_splits_at_the_danda(self):
        sentence = 'यह मेरी दुल्हन का पूरा लुक है जो हमने सुबह पाँच बजे शुरू किया।'
        a = hooks.beat_sheet(' '.join([sentence] * 3))
        assert [b['words'] for b in a['beats']] == [14, 14, 14]

    def test_a_long_hook_is_flagged_and_the_target_reported(self):
        script = ('one two three four five six seven eight nine ten eleven twelve\n'
                  'last line here')
        a = hooks.beat_sheet(script, target=30)
        assert any('past the 3s mark' in f for f in a['beats'][0]['flags'])
        assert any('under target' in n for n in a['notes'])

    def test_a_loop_word_is_found(self):
        a = hooks.beat_sheet('Bare face at 5 am.\nKryolan base.\nBare face to bride.')
        assert any(n.startswith('Loops:') for n in a['notes'])

    def test_an_empty_script(self):
        a = hooks.beat_sheet('  \n ')
        assert a['beats'] == [] and a['total_seconds'] == 0 and a['notes']

    @pytest.mark.parametrize('wpm', [0, -10, float('nan'), 'fast'])
    def test_a_bad_rate_is_refused(self, wpm):
        with pytest.raises(ValueError):
            hooks.beat_sheet('one line', wpm=wpm)

    def test_a_loop_word_with_many_digits_keeps_its_separators(self):
        a = hooks.beat_sheet('Rs 1500000 for the bridal look.\nWorth Rs 1500000.')
        assert any('1,500,000' in n for n in a['notes'] if n.startswith('Loops:'))


@pytest.mark.parametrize('unit', ['1,', '1.', '$1,', '1'])
def test_a_long_line_is_checked_in_linear_time(unit):
    # "The Statistic" ("\b\d[\d,.]*%") and "The Replacement" ("\$\d[^.!?]*
    # replaced") restarted at every digit or "$" of a run, and the beat
    # sheet's thousands separator scanned to the end from every digit: 7 s
    # and 4 s on 40,000 characters of "1,1,1,".
    import time
    text = unit * (40000 // len(unit))
    started = time.perf_counter()
    hooks.score_hooks([text])
    hooks.beat_sheet(text)
    assert time.perf_counter() - started < 3.0
