"""integrations.reel_studio.style: what her instructions say about the reel.

parse_style_hints reads her own words (the agent record she wrote when she
made her Reel Studio agent) for the reel's length, whether to open with a
glimpse of the final look (amendment A1: the story is chronological and a
<= 1 s final-look teaser is optional, ``hook_first``), the language, the
logo, its corner and the pace.  It returns only what it found, so a tool
argument or her voice profile decides the rest.

build_style puts the tool's arguments over those hints over her voice profile
over contracts.STYLE_DEFAULTS, and hands back what contracts.make_style
accepts.  agent_record_text reads her record through the one config reader
(core.cache_loaders.load_agent_config).
"""
import json

import pytest

from integrations.reel_studio import contracts as C
from integrations.reel_studio import style as S


# ── her example ───────────────────────────────────────────────────────────

def test_her_example_instructions():
    hints = S.parse_style_hints('open with the final look, 20s, Tamil songs, add my logo')
    assert hints == {'target_s': 20, 'hook_first': True, 'language': 'ta', 'use_logo': True}


def test_nothing_said_is_nothing_found():
    assert S.parse_style_hints('') == {}
    assert S.parse_style_hints(None) == {}
    assert S.parse_style_hints('Make lovely reels of my bridal work') == {}


# ── length ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,want', [
    ('a 20 sec reel', 20),
    ('keep it 15 seconds', 15),
    ('30-second reels', 30),
    ('about 25s please', 25),
    ('half a minute', 30),
    ('make it 12.5 s', 12.5),
])
def test_length_in_seconds(text, want):
    assert S.parse_style_hints(text)['target_s'] == want


def test_a_length_past_the_range_is_kept_as_said_and_clamped_by_the_style():
    assert S.parse_style_hints('a 1 minute reel')['target_s'] == 60
    assert S.build_style(hints={'target_s': 60})['target_s'] == C.TARGET_S_RANGE[1]


@pytest.mark.parametrize('text', [
    'brides in their 20s',
    'my clients are mostly in her 20s',
    'a 1990s look',
])
def test_an_age_or_a_decade_is_not_a_length(text):
    assert 'target_s' not in S.parse_style_hints(text)


# ── the opening (A1) ──────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'open with the final look',
    'Start with the result',
    'begin the reel with the reveal',
    'show the final look first, then the process',
    'end result first',
    'add a quick teaser of the final look',
    'kick off with the finished look',
])
def test_asking_for_the_final_look_first_sets_hook_first(text):
    assert S.parse_style_hints(text)['hook_first'] is True


@pytest.mark.parametrize('text', [
    "don't open with the final look",
    'do not start with the result',
    'never begin with the reveal',
    'no teaser please',
])
def test_saying_not_to_open_with_it_turns_it_off(text):
    assert S.parse_style_hints(text)['hook_first'] is False


@pytest.mark.parametrize('text', [
    'end with the final look',
    'start with the bare face',
    'show the whole look step by step',
])
def test_the_chronological_story_is_not_a_hook_request(text):
    assert 'hook_first' not in S.parse_style_hints(text)


# ── language ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,lang', [
    ('Tamil songs', 'ta'),
    ('தமிழ் பாடல்கள்', 'ta'),
    ('Hindi captions', 'hi'),
    ('हिंदी गाने', 'hi'),
    ('हिन्दी', 'hi'),
    ('punjabi beats', 'pa'),
    ('ਪੰਜਾਬੀ ਗੀਤ', 'pa'),
    ('English only', 'en'),
])
def test_language_by_name_in_any_script(text, lang):
    assert S.parse_style_hints(text)['language'] == lang


def test_the_first_language_named_wins():
    assert S.parse_style_hints('Tamil songs with English captions')['language'] == 'ta'


# ── logo ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,want', [
    ('add my logo', True),
    ('put our studio logo on it', True),
    ('with my watermark', True),
    ('no logo', False),
    ("don't add my logo", False),
    ('without the logo please', False),
])
def test_logo_on_or_off(text, want):
    assert S.parse_style_hints(text)['use_logo'] is want


def test_a_refused_logo_wins_over_a_mention():
    assert S.parse_style_hints('my logo is new, but no logo on reels')['use_logo'] is False


@pytest.mark.parametrize('text,corner', [
    ('logo in the top right corner', 'top_right'),
    ('put my logo upper-left', 'top_left'),
    ('bottom left logo', 'bottom_left'),
])
def test_logo_corner(text, corner):
    assert S.parse_style_hints(text)['logo_corner'] == corner


def test_a_corner_the_reel_cannot_use_is_ignored():
    # Instagram's buttons cover the bottom right; contracts.LOGO_CORNERS has no such corner.
    assert 'logo_corner' not in S.parse_style_hints('logo bottom right')


# ── pace ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,pace', [
    ('calm pace', 'calm'),
    ('slow-paced and soft', 'calm'),
    ('fast cuts', 'beat'),
    ('cut on the beat', 'beat'),
])
def test_pace(text, pace):
    assert S.parse_style_hints(text)['pace'] == pace


# ── build_style: who decides ──────────────────────────────────────────────

def test_defaults_alone_are_a_valid_style():
    style = S.build_style()
    assert style == C.make_style({})
    assert C.validate_style(style) == []


def test_tool_arguments_beat_her_instructions_which_beat_her_profile():
    profile = {'logo_corner': 'bottom_left', 'caption_language': 'hi'}
    hints = {'target_s': 25, 'hook_first': True, 'language': 'ta', 'logo_corner': 'top_right'}
    args = {'target_s': 15, 'hook_first': None, 'use_logo': False}
    style = S.build_style(args=args, hints=hints, profile=profile)
    assert style['target_s'] == 15                 # the argument
    assert style['hook_first'] is True             # None argument: her instructions decide
    assert style['use_logo'] is False              # the argument
    assert style['language'] == 'ta'               # her instructions over her caption language
    assert style['logo_corner'] == 'top_right'     # her instructions over the profile
    assert C.validate_style(style) == []


def test_the_profile_fills_what_her_instructions_do_not_say():
    style = S.build_style(profile={'logo_corner': 'bottom_left', 'caption_language': 'pa'})
    assert style['logo_corner'] == 'bottom_left' and style['language'] == 'pa'


def test_profile_values_the_style_cannot_take_are_left_out():
    style = S.build_style(profile={'caption_language': 'fr', 'logo_corner': 'middle'})
    assert style['language'] is None and style['logo_corner'] == 'top_left'


def test_an_unknown_argument_is_refused():
    with pytest.raises(ValueError):
        S.build_style(args={'colour': 'pink'})


# ── her agent record ──────────────────────────────────────────────────────

@pytest.fixture
def prompts_dir(tmp_path, monkeypatch):
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    return tmp_path


def test_record_text_holds_goal_flows_and_extra_information(prompts_dir):
    record = {'goal': 'Bridal reels for Meena Studio, 20s, Tamil songs',
              'flows': [{'flow_name': 'Reel', 'persona': 'Editor', 'sub_goal': 'open with the result',
                         'actions': ['Make the reel', 'add my logo top right']}],
              'extra_information': 'no teaser on reception looks'}
    (prompts_dir / '12345.json').write_text(json.dumps(record), encoding='utf-8')
    text = S.agent_record_text('12345')
    for part in ('Bridal reels for Meena Studio', 'open with the result', 'add my logo top right',
                 'no teaser on reception looks'):
        assert part in text
    assert S.agent_record_text(12345) == text      # prompt ids travel as int or str


def test_no_record_or_an_unsafe_id_is_no_text(prompts_dir):
    assert S.agent_record_text('999') == ''
    assert S.agent_record_text('../etc/passwd') == ''
    assert S.agent_record_text(None) == ''


def test_a_malformed_record_is_read_as_far_as_it_goes(prompts_dir):
    (prompts_dir / '7.json').write_text(json.dumps({'goal': 'calm pace', 'flows': ['}', 3]}),
                                        encoding='utf-8')
    assert S.agent_record_text('7') == 'calm pace'
