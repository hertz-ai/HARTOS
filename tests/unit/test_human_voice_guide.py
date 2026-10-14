"""The human-voice guide (goal_manager.HUMAN_VOICE_GUIDE) is the ONE source
of published-text style (owner ruling A10c, lane review P6).

* Hashtags: a reel caption may carry up to REEL_HASHTAG_LIMIT (5); every
  other published text keeps 0 to POST_HASHTAG_LIMIT (2).  The guide says
  both, and Reel Studio's contracts, caption linter and caption skill read
  the reel number from the guide, so there is no second literal to drift;
  so does every text her agent reads that states the cap (reel_make's
  hashtags parameter, her profile's hashtag-bank label and help).
* AI-sounding words: igkit's lexicon (its old resources/slop.json) lives in
  the guide's source.  The guide quotes a few of its tells; igkit's
  humanizer and detector read the whole list from there, and so does
  scripts/check_docs_claims.py (its FILLER list folded in, amendment A20
  D30), with the docs-only DOCS_TELLS beside it.  Every reader matches a
  tell through the guide's one tell_pattern.

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


def fresh_module(monkeypatch, name):
    """A new copy of module ``name``, executed now, so it reads what its
    imports hold at this moment (here: the guide) and starts with empty
    caches of its own (test_igkit_skills: the skill texts it caches on
    first read).  The module already imported did both at its first import.

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


def test_the_forms_the_docs_list_had_are_rewritten_too():
    # scripts/check_docs_claims.py's FILLER caught 'effortless' and
    # 'revolutionizing'; the list had only 'effortlessly' and 'revolutionize'.
    out = humanize.humanize_text('An effortless look, revolutionizing bridal glam.')
    assert out['text'] == 'An easy look, changing bridal glam.', out
    assert sorted(h['find'] for h in out['report']['lexical']) == ['effortless',
                                                                   'revolutionizing']


def test_the_docs_extension_adds_and_never_reaches_the_humanizer():
    # The words Hacker News counted in HARTOS's docs are docs-only: in her
    # caption "an honest review" is plain speech.
    hv = _guide()
    listed = {t.find.casefold() for t in hv.WORDS + hv.PHRASES}
    assert hv.DOCS_TELLS and not listed & {f.casefold() for f in hv.DOCS_TELLS}
    text = 'An honest review of a genuine lash glue that earns its keep.'
    assert humanize.humanize_text(text)['text'] == text


# ── the one matcher ─────────────────────────────────────────────────────────

def test_a_tell_matches_as_whole_words_in_any_case_across_spaces():
    pattern = _guide().tell_pattern('dive into')
    assert pattern.search('Time to DIVE   into it')
    assert pattern.search('dive\ninto')
    assert not pattern.search('skydive into') and not pattern.search('dive intolerance')
    leverage = _guide().tell_pattern('leverage')
    assert leverage.search('We Leverage it.') and not leverage.search('We leveraged it.')


def test_any_ending_finds_a_words_family_for_reports():
    hv = _guide()
    assert hv.tell_pattern('honest', any_ending=True).search('It is honestly fine.')
    assert not hv.tell_pattern('honest').search('It is honestly fine.')
    assert not hv.tell_pattern('honest', any_ending=True).search('It is dishonest.')


def test_the_humanizer_rewrites_exact_forms_only():
    # Each form has its own replacement: 'leveraged' is not 'leverage'.
    assert humanize.humanize_text('We leveraged it.')['text'] == 'We leveraged it.'


def test_a_tell_that_ends_on_punctuation_is_found_and_deleted():
    # 'what are your thoughts?' never matched before a space or the end:
    # "\b" after '?' needs a letter next.
    out = humanize.humanize_text('Love this look. What are your thoughts?')
    assert out['text'] == 'Love this look.', out
    assert [h['find'] for h in out['report']['lexical']] == ['what are your thoughts?']
    slop = humanize.detect_machine_text('Love this look. What are your thoughts?')
    assert slop['checks']['SLOP DENSITY']['detail'].startswith('1 stock terms'), slop
    assert not _guide().tell_pattern('what are your thoughts?').search(
        'what are your thoughts?x')


def test_the_humanizer_and_detector_match_through_the_one_matcher(monkeypatch):
    hv = _guide()
    asked = []
    real = hv.tell_pattern

    def spy(find, any_ending=False):
        asked.append((find, any_ending))
        return real(find, any_ending)

    monkeypatch.setattr(hv, 'tell_pattern', spy)
    humanize.humanize_text('We leverage it.')
    assert ('leverage', False) in asked
    asked.clear()
    humanize.detect_machine_text('We leverage it.')
    assert ('leverage', False) in asked


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
    contracts = fresh_module(monkeypatch, 'integrations.reel_studio.contracts')
    assert contracts.HASHTAG_LIMIT == 7
    assert contracts.validate_media_item(_media(_tags(7))) == []
    assert contracts.validate_media_item(_media(_tags(8)))


def test_the_caption_linter_takes_the_reel_hashtag_cap_from_the_guide(monkeypatch):
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    caption = fresh_module(monkeypatch, 'integrations.reel_studio.igkit.caption')
    r = caption.lint_caption('Reception look for Priya in 40 minutes.\n\n' + ' '.join(_tags(7)))
    assert r['hashtag_limit'] == 7
    assert [c['status'] for c in r['checks'] if c['check'] == 'HASHTAGS'] == ['PASS']


def test_the_caption_skill_names_the_guides_reel_cap(monkeypatch):
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    skills = fresh_module(monkeypatch, 'integrations.reel_studio.igkit.skills')
    text = skills.get_skill_text('ig_caption')
    described = {s['name']: s['description'] for s in skills.list_skills()}['ig_caption']
    assert 'up to 7' in text and 'up to 7' in described
    assert 'five' not in text and '{' not in text and '{' not in described


def test_her_profile_takes_the_reel_hashtag_cap_from_the_guide(monkeypatch):
    # Her template writes the guide's token, never a number of its own; the
    # profile's label (her brief) and help (reel_voice fields) say the cap.
    real = _guide().REEL_HASHTAG_LIMIT
    raw = load_json('voice_template.json')['fields']['hashtag_bank']
    assert not re.search(r'\b%d\b' % real, raw['label'] + ' ' + raw['help']), raw
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    voice_profile = fresh_module(monkeypatch, 'integrations.reel_studio.voice_profile')
    spec = voice_profile.FIELD_SPECS['hashtag_bank']
    assert 'up to 7' in spec['label'] and 'up to 7' in spec['help'], dict(spec)
    for field, spec in voice_profile.FIELD_SPECS.items():
        said = spec.get('label', '') + ' ' + spec.get('help', '')
        assert '{' not in said, (field, said)


def test_her_brief_tells_her_agent_the_reel_hashtag_cap(tmp_path, monkeypatch):
    # The brief reel_skill puts in front of every skill: her hashtag bank
    # comes with the cap, so the agent does not offer tags the reel drops.
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('HEVOLVE_DATA_KEY', raising=False)
    import security.secrets_manager as secrets_manager
    monkeypatch.setattr(secrets_manager, 'get_secret', lambda name, default='': default)
    from core import platform_paths
    platform_paths.reset_cache()
    try:
        from integrations.reel_studio import voice_profile
        voice_profile.update_field('10077', 'hashtag_bank', '#maduraimua #bridalmakeup')
        brief = voice_profile.voice_brief('10077')
    finally:
        platform_paths.reset_cache()
    line = [ln for ln in brief.splitlines() if '#maduraimua' in ln]
    assert len(line) == 1 and 'up to %d' % _guide().REEL_HASHTAG_LIMIT in line[0], brief


def test_reel_make_tells_the_model_the_cap_it_clamps_to(monkeypatch):
    # What the model reads in reel_make's schema and what reel_make keeps
    # are the same number: the guide's, through contracts.HASHTAG_LIMIT.
    pytest.importorskip('autogen')
    from autogen.function_utils import get_function_schema
    monkeypatch.setattr(_guide(), 'REEL_HASHTAG_LIMIT', 7)
    fresh_module(monkeypatch, 'integrations.reel_studio.contracts')
    from integrations.reel_studio import tools
    reel_make = dict(tools.build_reel_tools('10077', prompt_id='42'))['reel_make']
    schema = get_function_schema(reel_make, name='reel_make', description='make a reel')
    said = schema['function']['parameters']['properties']['hashtags']['description']
    assert 'Up to 7' in said and '5' not in said and '{' not in said, said
    assert tools._hashtags(' '.join(_tags(8)), {}) == _tags(7)
