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
ZWJ = '‍'


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
