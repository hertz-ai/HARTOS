"""Unit tests for integrations/social/media_curator_service (closes #401).

Pure-function classifier tests — no DB, no mocks on production.  The
optional `llm_callback` is a real callable test parameter: tests pass
real lambdas exercising the contract.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.social.media_curator_service import (  # noqa: E402
    INTENT_APPROVE,
    INTENT_CAPTION_STYLE,
    INTENT_CHANNELS,
    INTENT_REJECT,
    INTENT_SCHEDULE,
    INTENT_UNKNOWN,
    CuratorIntent,
    parse_curator_command,
)


# ══════════════════════════════════════════════════════════════════════
# Approve / reject vocabulary
# ══════════════════════════════════════════════════════════════════════


def test_approve_post_this():
    out = parse_curator_command('post this')
    assert out.kind == INTENT_APPROVE
    assert out.confidence > 0.7


def test_approve_share_it():
    out = parse_curator_command('share it')
    assert out.kind == INTENT_APPROVE


def test_approve_looks_great():
    out = parse_curator_command('this looks great')
    assert out.kind == INTENT_APPROVE


def test_approve_keep_this():
    assert parse_curator_command('keep this').kind == INTENT_APPROVE


def test_reject_skip_it():
    out = parse_curator_command('skip it')
    assert out.kind == INTENT_REJECT
    assert out.confidence > 0.7


def test_reject_not_this():
    assert parse_curator_command('not this').kind == INTENT_REJECT


def test_reject_pass():
    assert parse_curator_command('pass').kind == INTENT_REJECT


def test_reject_drop_it():
    assert parse_curator_command('drop it').kind == INTENT_REJECT


# ══════════════════════════════════════════════════════════════════════
# Caption style
# ══════════════════════════════════════════════════════════════════════


def test_caption_with_hiking_vibe():
    out = parse_curator_command('caption with hiking vibe')
    assert out.kind == INTENT_CAPTION_STYLE
    assert out.payload['style'] == 'hiking'


def test_caption_with_indie_film_tone():
    out = parse_curator_command('caption with indie_film tone')
    assert out.kind == INTENT_CAPTION_STYLE
    assert out.payload['style'] == 'indie_film'


def test_caption_in_minimalist_style():
    out = parse_curator_command('caption in minimalist style')
    assert out.kind == INTENT_CAPTION_STYLE
    assert out.payload['style'] == 'minimalist'


def test_caption_filler_only_does_not_match_caption_style():
    """'caption it' alone (filler pronouns, no real style word) must
    fall through — the filler-word guard rejects it/this/that as
    styles."""
    out = parse_curator_command('caption it')
    assert out.kind != INTENT_CAPTION_STYLE


def test_caption_it_with_real_word_captures_style():
    """'caption it nicely' captures 'nicely' as the style hint.
    Adverbs / adjectives are valid style descriptors — only
    structural fillers ('it', 'this', 'that', 'a', 'with', 'in')
    are rejected."""
    out = parse_curator_command('caption it nicely')
    assert out.kind == INTENT_CAPTION_STYLE
    assert out.payload['style'] == 'nicely'


# ══════════════════════════════════════════════════════════════════════
# Schedule (more specific than approve — beats it in classifier order)
# ══════════════════════════════════════════════════════════════════════


def test_schedule_friday_morning_beats_approve():
    """'post Friday morning' has 'post' which would match approve,
    but the caption/schedule classifiers run first and capture it."""
    out = parse_curator_command('post Friday morning')
    assert out.kind == INTENT_SCHEDULE
    assert out.payload['day'] == 'friday'
    assert out.payload['time_of_day'] == 'morning'


def test_schedule_relative_tomorrow():
    out = parse_curator_command('post tomorrow')
    assert out.kind == INTENT_SCHEDULE
    assert out.payload['relative'] == 'tomorrow'


def test_schedule_just_a_time_of_day():
    out = parse_curator_command('share it tonight')
    assert out.kind == INTENT_SCHEDULE
    assert out.payload['relative'] == 'tonight'


def test_schedule_confidence_scales_with_specificity():
    one = parse_curator_command('post saturday')
    two = parse_curator_command('post saturday morning')
    assert two.confidence > one.confidence


# ══════════════════════════════════════════════════════════════════════
# Channels
# ══════════════════════════════════════════════════════════════════════


def test_channels_single_platform():
    out = parse_curator_command('share on linkedin')
    assert out.kind == INTENT_CHANNELS
    assert out.payload['channels'] == ['linkedin']


def test_channels_multiple_platforms():
    out = parse_curator_command('post on twitter and linkedin')
    assert out.kind == INTENT_CHANNELS
    assert set(out.payload['channels']) == {'twitter', 'linkedin'}


def test_channels_x_alias_normalizes_to_twitter():
    out = parse_curator_command('share on x.com')
    assert out.kind == INTENT_CHANNELS
    assert out.payload['channels'] == ['twitter']


# ══════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════


def test_empty_string_returns_unknown():
    out = parse_curator_command('')
    assert out.kind == INTENT_UNKNOWN
    assert out.confidence == 0.0


def test_whitespace_only_returns_unknown():
    out = parse_curator_command('   \n\t   ')
    assert out.kind == INTENT_UNKNOWN


def test_unrecognised_text_returns_unknown():
    out = parse_curator_command('the quick brown fox')
    assert out.kind == INTENT_UNKNOWN
    assert out.raw_text == 'the quick brown fox'


def test_returned_object_is_curator_intent_dataclass():
    out = parse_curator_command('skip')
    assert isinstance(out, CuratorIntent)
    assert out.raw_text == 'skip'


# ══════════════════════════════════════════════════════════════════════
# LLM callback contract (same fall-through as draft_icebreaker)
# ══════════════════════════════════════════════════════════════════════


def test_llm_callback_used_when_returns_valid_intent():
    def cb(raw):
        return {
            'kind': INTENT_APPROVE,
            'payload': {'override_via_llm': True},
            'confidence': 0.99,
        }

    out = parse_curator_command('whatever', llm_callback=cb)
    assert out.kind == INTENT_APPROVE
    assert out.payload['override_via_llm'] is True
    assert out.confidence == 0.99


def test_llm_callback_invalid_kind_falls_back():
    out = parse_curator_command(
        'skip it',
        llm_callback=lambda raw: {'kind': 'invented_kind'},
    )
    assert out.kind == INTENT_REJECT  # fell back to deterministic


def test_llm_callback_raises_falls_back():
    def cb(raw):
        raise RuntimeError('llm down')

    out = parse_curator_command('post this', llm_callback=cb)
    assert out.kind == INTENT_APPROVE


def test_llm_callback_returns_non_dict_falls_back():
    out = parse_curator_command(
        'pass',
        llm_callback=lambda raw: 42,
    )
    assert out.kind == INTENT_REJECT
