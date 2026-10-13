"""integrations.reel_studio.igkit.caption: the caption linter, ported from
instagram-agent-skill (MIT).

Upstream's hashtag pattern was ASCII-only, so a Tamil hashtag was cut at its
first vowel sign or not counted (skill-port report (c)3).  The port counts
hashtags in any script against the ONE hashtag cap (contracts.HASHTAG_LIMIT)
and knows the booking asks Indian makeup artists use.
"""
import pytest

from integrations.reel_studio import contracts
from integrations.reel_studio.igkit import caption

CLEAN = ('Reception look in 40 minutes, Chennai humidity and all.\n\n'
         'Kryolan base that held for 9 hours of dancing, MAC lips, her own temple jewellery.\n\n'
         'DM for bookings.\n\n'
         '#chennaimua #bridalmakeup')


def _check(result, name):
    return next(c for c in result['checks'] if c['check'] == name)


def test_tamil_hashtags_are_whole_and_counted():
    text = ('Bridal look for a December muhurtham in Madurai. DM for bookings.\n\n'
            '#சென்னைமேக்கப் #bridalmakeup #மணப்பெண்')
    r = caption.lint_caption(text)
    assert r['hashtags'] == ['#சென்னைமேக்கப்', '#bridalmakeup', '#மணப்பெண்']


def test_the_hashtag_cap_is_the_contract_limit():
    body = 'Reception look, 12-hour wear in Chennai humidity. DM for bookings.\n\n'
    at_cap = body + ' '.join('#look%d' % i for i in range(contracts.HASHTAG_LIMIT))
    over = body + ' '.join('#look%d' % i for i in range(contracts.HASHTAG_LIMIT + 1))
    assert _check(caption.lint_caption(at_cap), 'HASHTAGS')['status'] == 'PASS'
    assert _check(caption.lint_caption(over), 'HASHTAGS')['status'] == 'FAIL'


def test_a_tighter_hashtag_limit_can_be_passed():
    r = caption.lint_caption('Look one in 40 minutes.\n\n#a1 #b2 #c3 #d4', hashtag_limit=3)
    assert _check(r, 'HASHTAGS')['status'] == 'FAIL'


def test_generic_tags_are_named():
    r = caption.lint_caption('Look one in 40 minutes.\n\n#bridal #viral #reelitfeelit')
    check = _check(r, 'HASHTAGS')
    assert check['status'] == 'WARN' and '#viral' in check['detail']


@pytest.mark.parametrize('ask_line,ask', [
    ('DM for bookings.', 'DM for bookings'),
    ('Bookings open for December.', 'bookings open'),
    ('WhatsApp 98765 43210 for dates.', 'WhatsApp number'),
    ('WhatsApp me for bookings.', 'DM for bookings'),
])
def test_indian_booking_asks_are_one_ask(ask_line, ask):
    r = caption.lint_caption('Sangeet look in 40 minutes.\n' + ask_line)
    assert r['asks'] == [ask]
    assert _check(r, 'ONE ASK')['status'] == 'PASS'


def test_two_asks_warn():
    r = caption.lint_caption('Sangeet look in 40 minutes.\nSave this and DM for bookings.')
    assert _check(r, 'ONE ASK')['status'] == 'WARN'


def test_a_phone_number_is_not_a_link():
    r = caption.lint_caption('Sangeet look in 40 minutes.\nWhatsApp 98765 43210 for dates.')
    assert r['links'] == []
    assert _check(r, 'LINKS')['status'] == 'PASS'


def test_a_link_warns():
    r = caption.lint_caption('Sangeet look in 40 minutes.\nBook at www.example.com/book now.')
    assert _check(r, 'LINKS')['status'] == 'WARN'


def test_search_terms_in_any_script():
    r = caption.lint_caption('மதுரை bridal makeup artist, முகூர்த்தம் லுக்.\nDM for bookings.',
                             keywords=['மதுரை', 'Bridal Makeup Artist'])
    assert _check(r, 'SEARCH TERMS')['status'] == 'PASS'
    r = caption.lint_caption('Bridal look.\nDM for bookings.', keywords=['மதுரை'])
    assert _check(r, 'SEARCH TERMS')['status'] == 'FAIL'


def test_a_first_line_hashtag_fails():
    r = caption.lint_caption('#bridal look for Priya\nDM for bookings.')
    assert _check(r, 'FIRST LINE')['status'] == 'FAIL'
    assert r['verdict'] == 'FIX'


def test_the_visible_window_is_what_the_feed_shows():
    r = caption.lint_caption('x' * 130)
    assert r['visible'] == 'x' * 125 and r['truncated'] is True
    assert caption.lint_caption('x' * 130, truncate=200)['truncated'] is False


def test_a_clean_caption_is_ready():
    r = caption.lint_caption(CLEAN)
    assert r['verdict'] == 'READY', r['checks']
    assert r['hashtags'] == ['#chennaimua', '#bridalmakeup']
    assert _check(r, 'TAG PLACEMENT')['status'] == 'PASS'


def test_over_the_caption_limit_fails():
    r = caption.lint_caption('Look in 40 minutes. ' + 'a' * caption.LIMIT)
    assert _check(r, 'LENGTH')['status'] == 'FAIL'
