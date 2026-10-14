"""The human-voice guide (goal_manager.HUMAN_VOICE_GUIDE) is the ONE source
of published-text style (owner ruling A10c, lane review P6).

* Hashtags: a reel caption may carry up to REEL_HASHTAG_LIMIT (5); every
  other published text keeps 0 to POST_HASHTAG_LIMIT (2).  The guide says
  both, and Reel Studio's contracts, caption linter and caption skill read
  the reel number from the guide, so there is no second literal to drift.
* AI-sounding words: igkit's lexicon (its old resources/slop.json) lives in
  the guide's source.  The guide quotes a few of its tells; igkit's
  humanizer and detector read the whole list from there.

The guide's module is imported inside each test, so a test that needs it
fails on its own instead of failing the whole file at collection.
"""
import importlib
import importlib.util
import re
import sys

import pytest

from integrations.reel_studio.igkit import humanize
from integrations.reel_studio.igkit.resources import load_json

SHA = 'ab' * 32

#: The tells the guide banned by name before the fold.  Each must still be
#: named by the guide and caught by igkit (the reworded ones in their
#: lexicon form: "...landscape of", "it's worth noting that", "harness the
#: power of").
GUIDE_TELLS_BEFORE_THE_FOLD = (
    'in the rapidly evolving landscape of', "in today's world", 'delve', 'dive in',
    'dive into', "it's worth noting that", 'in conclusion', 'moreover', 'furthermore',
    'leverage', 'seamless', 'game-changer', 'revolutionary', 'cutting-edge', 'unlock',
    'elevate', 'harness the power of', 'the power of', 'robust', 'transformative',
    'unleash')


def _guide():
    return importlib.import_module('integrations.agent_engine.human_voice')


def _fresh(monkeypatch, name):
    """A new copy of module ``name``, executed now, so it reads what the
    guide holds at this moment (the module already imported read it at its
    first import).

    The copy stands in sys.modules under ``name`` while it runs and for the
    rest of the test, the way an import registers a module before running
    it: a dataclass in the module (contracts has frozen ones) looks its own
    module up there while the class is built.  monkeypatch puts back what
    was there afterwards, the real module, or nothing if it was never
    imported, so no other test sees the copy."""
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _media(tags):
    return {'kind': 'video', 'xid': SHA, 'sha256': SHA, 'mime': 'video/mp4', 'size': 1000,
            'width': 1080, 'height': 1920, 'duration_s': 20.0, 'has_audio': False,
            'reel_id': 'r1', 'prompt_id': '1234', 'request_id': None,
            'caption': 'Bridal glow', 'hashtags': tags, 'song': None, 'notes': [],
            'qa': {'passed': True, 'failed': []}}


def _tags(n):
    return ['#look%d' % i for i in range(n)]


# ── the guide ───────────────────────────────────────────────────────────────

def test_the_guide_gives_reels_up_to_five_hashtags_and_everything_else_two():
    hv = _guide()
    assert (hv.REEL_HASHTAG_LIMIT, hv.POST_HASHTAG_LIMIT) == (5, 2)
    guide = hv.HUMAN_VOICE_GUIDE
    assert 'At most 2 hashtags, or none.' in guide
    assert 'a reel caption may carry up to 5 hashtags' in guide
    assert 'Zero to two hashtags' not in guide


def test_every_goal_prompt_that_carries_the_guide_carries_the_one_guide():
    hv = _guide()
    from integrations.agent_engine import goal_manager
    assert goal_manager.HUMAN_VOICE_GUIDE is hv.HUMAN_VOICE_GUIDE
    prompt = goal_manager._build_news_prompt({'title': 'Chennai news', 'description': 'local'})
    assert hv.HUMAN_VOICE_GUIDE in prompt
    assert 'a reel caption may carry up to 5 hashtags' in prompt


def test_the_guide_still_bans_its_tells_by_name_and_igkit_catches_each():
    hv = _guide()
    for tell in GUIDE_TELLS_BEFORE_THE_FOLD:
        assert "'%s'" % tell in hv.HUMAN_VOICE_GUIDE, tell
        found = [h['find'] for h in humanize.humanize_text('We %s it.' % tell)['report']['lexical']]
        assert tell in found, (tell, found)


def test_a_tell_the_guide_named_but_igkit_did_not_know_is_now_replaced():
    # 'revolutionary' was in the guide and missing from slop.json: two lists.
    out = humanize.humanize_text('A revolutionary primer for her reception look.')
    assert out['text'] == 'A new primer for her reception look.'
    assert [h['find'] for h in out['report']['lexical']] == ['revolutionary']


def test_every_tell_the_guide_quotes_comes_from_the_one_list():
    hv = _guide()
    quoted = [t for t in hv.WORDS + hv.PHRASES if t.quoted]
    assert sorted(t.find for t in quoted) == sorted(GUIDE_TELLS_BEFORE_THE_FOLD)
    listed = re.search(r"Ban these tells and their close cousins: (.*?)\. No ",
                       hv.HUMAN_VOICE_GUIDE).group(1)
    assert listed == ', '.join("'%s'" % t.find for t in quoted)


def test_the_guide_stays_short_enough_for_a_local_model():
    # The local ~4B model runs these prompts; it quotes a few tells, never
    # the whole lexicon.
    hv = _guide()
    assert len(hv.HUMAN_VOICE_GUIDE) < 3000
    assert len(hv.WORDS) + len(hv.PHRASES) > 100
    assert '—' not in hv.HUMAN_VOICE_GUIDE.replace(
        "never use an em dash (the '—' character)", '')


# ── the one list ────────────────────────────────────────────────────────────

def test_igkit_ships_no_second_lexicon():
    with pytest.raises(KeyError):
        load_json('slop.json')


def test_igkit_reads_its_lexicon_from_the_guide(monkeypatch):
    hv = _guide()
    lex = hv.machine_text_lexicon()
    words = tuple(lex['words']) + ({'find': 'glamazing', 'replace': 'lovely',
                                    'family': 'adjectives'},)
    monkeypatch.setattr(hv, 'machine_text_lexicon', lambda: dict(lex, words=words))
    assert humanize.humanize_text('A glamazing look.')['text'] == 'A lovely look.'
    slop = humanize.detect_machine_text('A glamazing look.')['checks']['SLOP DENSITY']
    assert slop['detail'].startswith('1 stock terms')


def test_the_list_is_sound_and_read_only():
    lex = _guide().machine_text_lexicon()
    assert set(lex) == {'invisible', 'typographic', 'words', 'phrases', 'structures'}
    finds = [e['find'].casefold() for e in tuple(lex['words']) + tuple(lex['phrases'])]
    assert len(finds) == len(set(finds)), 'a tell is listed twice'
    for s in lex['structures']:
        re.compile(s['regex'])
    for entry in lex['invisible']:
        assert entry['action'] in ('delete', 'space', 'keep_joining')
        humanize._cp(entry['cp'])
    with pytest.raises(TypeError):
        lex['words'] = ()
    with pytest.raises(TypeError):
        lex['words'][0]['replace'] = 'x'


def test_the_folded_list_keeps_igkits_rules():
    out = humanize.humanize_text("Reception look ✨ for Priya. It's not just makeup, "
                                 "it's confidence.\n\n#a1 #b2 #c3 #d4 #e5")
    ids = [s['id'] for s in out['report']['structures']]
    assert 'rocket' in ids and 'not-just' in ids
    assert not [i for i in ids if 'hashtag' in i]
    assert 'rocket' not in [s['id'] for s in humanize.humanize_text(
        'Reception look ✨ for Priya.', keep_emoji=['✨'])['report']['structures']]


# ── the reel number has one home ────────────────────────────────────────────

def test_contracts_take_the_reel_hashtag_cap_from_the_guide(monkeypatch):
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    contracts = _fresh(monkeypatch, 'integrations.reel_studio.contracts')
    assert contracts.HASHTAG_LIMIT == 7
    assert contracts.validate_media_item(_media(_tags(7))) == []
    assert contracts.validate_media_item(_media(_tags(8)))


def test_the_caption_linter_takes_the_reel_hashtag_cap_from_the_guide(monkeypatch):
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    caption = _fresh(monkeypatch, 'integrations.reel_studio.igkit.caption')
    r = caption.lint_caption('Reception look for Priya in 40 minutes.\n\n' + ' '.join(_tags(7)))
    assert r['hashtag_limit'] == 7
    assert [c['status'] for c in r['checks'] if c['check'] == 'HASHTAGS'] == ['PASS']


def test_the_caption_skill_names_the_guides_reel_cap(monkeypatch):
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    skills = _fresh(monkeypatch, 'integrations.reel_studio.igkit.skills')
    text = skills.get_skill_text('ig_caption')
    described = {s['name']: s['description'] for s in skills.list_skills()}['ig_caption']
    assert 'up to 7' in text and 'up to 7' in described
    assert 'five' not in text and '{' not in text and '{' not in described


def test_her_profile_states_no_hashtag_cap_of_its_own():
    from integrations.reel_studio import voice_profile
    spec = voice_profile.FIELD_SPECS['hashtag_bank']
    said = spec['label'] + ' ' + spec['help']
    assert not re.search(r'\b%d\b' % _guide().REEL_HASHTAG_LIMIT, said), said
