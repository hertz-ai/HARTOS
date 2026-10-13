"""integrations.reel_studio.igkit.humanize: the humanizer and the five-check
detection panel, ported from instagram-agent-skill (MIT).

Upstream deleted every zero-width joiner, which broke the bride emoji, the
family emoji, Malayalam chillu letters and Hindi conjuncts; it scored any
Hindi paragraph REVIEW because it counted no Hindi words or danda sentences;
it flagged ✨ (a staple of makeup posts) and five hashtags (her cap) as AI
tells (skill-port report findings 4 and (c)2-4).  These tests pin the port.
"""
import pathlib

import pytest

from integrations.reel_studio.igkit import humanize

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'igkit'
ZWJ = '‍'
ZWNJ = '‌'


def _fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def _ids(report):
    return [s['id'] for s in report['structures']]


class TestHumanize:
    def test_joiners_in_emoji_and_indic_letters_are_kept(self):
        text = ('Bride 👰' + ZWJ + '♀️ ready, family 👨' + ZWJ + '👩' + ZWJ + '👧, '
                'क्' + ZWJ + 'ष and ന്' + ZWJ + ' and क्' + ZWNJ + 'ष')
        out = humanize.humanize_text(text)
        assert out['text'].count(ZWJ) == 5
        assert out['text'].count(ZWNJ) == 1
        assert out['report']['invisible'] == []

    def test_a_joiner_between_latin_letters_is_a_watermark(self):
        out = humanize.humanize_text('wa' + ZWJ + 'ter​proof base')
        assert out['text'] == 'waterproof base'
        assert {h['count'] for h in out['report']['invisible']} == {1}
        assert len(out['report']['invisible']) == 2

    def test_em_dash_and_stock_words(self):
        out = humanize.humanize_text('This look is stunning — it will elevate your day.')
        assert out['text'] == 'This look is good, it will improve your day.'
        assert sorted(h['find'] for h in out['report']['lexical']) == ['elevate', 'stunning']

    def test_links_are_left_alone(self):
        out = humanize.humanize_text('Leverage this — www.example.com/leverage—now')
        assert 'www.example.com/leverage—now' in out['text']
        assert out['text'].startswith('Use this,')

    def test_sparkle_is_kept_when_she_uses_it(self):
        text = 'Reception look ✨ for Priya, 40 minutes.'
        assert 'rocket' in _ids(humanize.humanize_text(text)['report'])
        kept = humanize.humanize_text(text, keep_emoji=['✨'])
        assert 'rocket' not in _ids(kept['report'])
        assert '✨' in kept['text']

    def test_an_allowlist_does_not_hide_other_emoji(self):
        out = humanize.humanize_text('Reception look 🔥 for Priya.', keep_emoji=['✨'])
        assert 'rocket' in _ids(out['report'])

    def test_words_she_uses_are_kept(self):
        text = 'Stunning reception look, 40 minutes.'
        assert humanize.humanize_text(text)['text'] == 'Good reception look, 40 minutes.'
        kept = humanize.humanize_text(text, keep_words=['stunning'])
        assert kept['text'] == text and kept['report']['lexical'] == []

    def test_five_hashtags_are_not_a_wall(self):
        out = humanize.humanize_text('Reception look.\n\n#a1 #b2 #c3 #d4 #e5')
        assert not [i for i in _ids(out['report']) if 'hashtag' in i]

    def test_structures_are_reported_not_rewritten(self):
        text = "It's not just makeup, it's confidence."
        out = humanize.humanize_text(text)
        assert 'not-just' in _ids(out['report'])
        assert out['text'] == text

    def test_hindi_text_is_left_as_written(self):
        text = _fixture('hindi_paragraph.txt').strip()
        out = humanize.humanize_text(text)
        assert out['text'] == text


class TestDetect:
    def test_a_human_hindi_paragraph_passes(self):
        r = humanize.detect_machine_text(_fixture('hindi_paragraph.txt'))
        assert r['verdict'] == 'PASS', r
        assert all('too short' not in c['detail'] for c in r['checks'].values())
        assert 'across 7 sentences' in r['checks']['BURSTINESS']['detail']
        # Hindi has no English contractions to count; scoring it as if it
        # did would cap VOICE near 64 however personal the text is.
        assert r['checks']['VOICE']['score'] >= 90.0

    def test_her_words_do_not_count_as_slop(self):
        text = ('Stunning reception look for Priya in 40 minutes, Chennai humidity and all. '
                'Kryolan base, MAC lips, and she cried when she saw the mirror. So did I. '
                'Best day of my month, and you could see it on her face.')
        plain = humanize.detect_machine_text(text)['checks']['SLOP DENSITY']
        kept = humanize.detect_machine_text(text, keep_words=['stunning'])
        assert plain['detail'].startswith('1 stock terms') and plain['score'] < 100.0
        assert kept['checks']['SLOP DENSITY']['score'] == 100.0

    def test_machine_english_is_still_flagged(self):
        r = humanize.detect_machine_text(_fixture('machine_draft.txt'))
        assert r['verdict'] in ('REVIEW', 'FLAGGED'), r
        assert r['checks']['SLOP DENSITY']['score'] < 55

    def test_before_and_after(self):
        r = humanize.detect_machine_text(_fixture('hindi_paragraph.txt'),
                                         before=_fixture('machine_draft.txt'))
        assert r['delta'] == pytest.approx(r['human_score'] - r['before']['human_score'],
                                           abs=0.11)
        assert r['delta'] > 0
        assert r['before']['verdict'] in ('REVIEW', 'FLAGGED')

    def test_emoji_and_indic_joiners_are_not_a_fingerprint(self):
        r = humanize.detect_machine_text('Bride 👰' + ZWJ + '♀️ ready, क्' + ZWJ + 'ष done')
        assert r['checks']['FINGERPRINT']['score'] == 100.0
        r = humanize.detect_machine_text('ready' + ZWJ + 'now')
        assert r['checks']['FINGERPRINT']['score'] < 100.0

    def test_scores_are_bounded_and_named(self):
        r = humanize.detect_machine_text('')
        assert set(r['checks']) == set(humanize.DETECT_CHECKS)
        assert all(0.0 <= c['score'] <= 100.0 for c in r['checks'].values())
        assert r['verdict'] in ('PASS', 'REVIEW', 'FLAGGED')
