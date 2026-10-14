"""reel_edit: Reel Studio's own reel-editing skill (owner ruling A15), read by
her agent through reel_skill.

It sits in the Reel Studio skill package beside ig_reel, ig_caption and
ig_human and is loaded the same way (package data parsed with the skill
registry's parser, at most MAX_SKILL_CHARS, single-line frontmatter, never
registered in the global skill registry: tests/unit/test_igkit_skills.py
runs those checks over every name in SKILL_NAMES).  Unlike those three it is
not adapted from the MIT instagram-agent-skill port: it is HARTOS's own text
(Apache-2.0), written for HARTOS.  OpenMontage (AGPL-3.0) was read as a
reference only; nothing of it is in the text.

What is checked here:

* its frontmatter says it is HARTOS's own text, not part of the MIT port;
* it reads the way the human-voice guide asks published text to read (the
  guide is the one style source): the guide's own checker finds nothing to
  clean or rewrite in it;
* every figure it gives her agent about the cut (seconds, the steady beat's
  BPM, the on-screen line's words) is the planner's own value, so the text
  cannot promise a reel the planner does not make;
* it reaches her Reel Studio agent in CREATE and REUSE: the real
  constructors build her agent, reel_skill's schema names it to the model,
  and the Executor's reel_skill returns it with her voice brief in front.

    python -m pytest tests/unit/test_reel_edit_skill.py -q -p no:cacheprovider
"""
import os
import re

import pytest

from tests.unit.test_create_loop_end_to_end import USER_ID, create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, seats)

SKILL = 'reel_edit'
#: A figure the text gives: a number or a range, then its unit.
_FIGURE = re.compile(r'(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?\s*(s|BPM|words)\b')


def _skills():
    from integrations.reel_studio.igkit import skills
    return skills


def _frontmatter():
    from integrations.skills import registry
    path = os.path.join(os.path.dirname(_skills().__file__), SKILL, 'SKILL.md')
    with open(path, encoding='utf-8') as fh:
        meta, _body = registry._parse_frontmatter(fh.read())
    return meta


def _figures(text):
    """``{unit: {value, ...}}`` for every figure in ``text``."""
    said = {}
    for m in _FIGURE.finditer(text):
        for value in (m.group(1), m.group(2)):
            if value is not None:
                said.setdefault(m.group(3), set()).add(float(value))
    return said


def test_it_is_one_of_the_skills_her_agent_can_read():
    skills = _skills()
    assert SKILL in skills.SKILL_NAMES
    assert SKILL in [s['name'] for s in skills.list_skills()]
    text = skills.get_skill_text(SKILL)
    assert text.startswith('# ' + SKILL)
    assert skills.get_skill_text(' Reel-Edit ') == text


def test_it_is_hartos_own_text_not_part_of_the_mit_port():
    """Owner ruling A15: our own words, under HARTOS's license.  The three
    adapted skills name the MIT upstream in their license line; this one
    must not, or it would claim a provenance it does not have."""
    licence = _frontmatter()['license']
    assert licence.startswith('Apache-2.0'), licence
    assert 'MIT' not in licence and 'adapted' not in licence.lower(), licence


def test_it_reads_the_way_the_human_voice_guide_asks():
    """The guide (integrations.agent_engine.human_voice) is the one style
    source: the checker built on its list (igkit.humanize.humanize_text)
    finds no invisible or typographic character, no stock word and no
    machine sentence shape to rewrite, and leaves the text as it is."""
    from integrations.reel_studio.igkit.humanize import humanize_text
    text = _skills().get_skill_text(SKILL)
    cleaned = humanize_text(text)
    assert cleaned['report'] == {'invisible': [], 'typographic': [], 'lexical': [],
                                 'structures': []}, cleaned['report']
    assert '\N{EM DASH}' not in text


def test_every_figure_it_gives_is_the_planners_own():
    """Her agent repeats these to her; a figure the planner does not use
    would promise a reel reel_make does not make (the reveal's hold, the
    teaser, the length range, the on-screen line, the pace, the steady
    beat)."""
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.igkit.hooks import CARD_MAX_WORDS
    from integrations.reel_studio.media import beats, edl
    planner_seconds = {float(v) for v in (
        *C.TARGET_S_RANGE, C.DEFAULT_TARGET_S, C.QUALITY_BAR['reveal']['min_hold_s'],
        C.QUALITY_BAR['teaser']['max_s'], *edl.REVEAL_IDEAL_S, edl.HOOK_TEXT_S,
        *edl.SHOT_S.values())}
    said = _figures(_skills().get_skill_text(SKILL))
    assert set(said) == {'s', 'BPM', 'words'}, said
    assert said['s'] <= planner_seconds, sorted(said['s'] - planner_seconds)
    assert said['BPM'] == {float(beats.SYNTHETIC_BPM)}
    assert said['words'] == {float(CARD_MAX_WORDS)}


def test_the_figure_check_sees_a_figure_the_planner_does_not_use():
    """Anti-vacuity for the check above."""
    said = _figures('Hold the reveal 7 s, cut at 90 BPM, a 5-30 s reel, 8 words.')
    assert said == {'s': {7.0, 5.0, 30.0}, 'BPM': {90.0}, 'words': {8.0}}


@pytest.fixture
def her_desktop(monkeypatch):
    """Her voice profile in plain JSON under the seats' data dir (a stock
    desktop has no HEVOLVE_DATA_KEY)."""
    monkeypatch.delenv('HEVOLVE_DATA_KEY', raising=False)
    import security.secrets_manager as secrets_manager
    monkeypatch.setattr(secrets_manager, 'get_secret', lambda name, default='': default)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_her_agent_is_told_of_it_and_reads_it(seats, her_desktop, pipeline):
    from core.constants import tool_reply_failed
    from integrations.reel_studio import voice_profile
    skills = _skills()
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    # What the model is shown: reel_skill's name parameter names every skill
    # her agent can read, this one among them.
    for seat in (built.helper, built.assistant):
        tool = next(t['function'] for t in seat.llm_config['tools']
                    if t['function']['name'] == 'reel_skill')
        said = tool['parameters']['properties']['name']['description']
        assert SKILL in said, (pipeline, seat.name, said)
        assert [n for n in skills.SKILL_NAMES if n not in said] == [], (pipeline, seat.name,
                                                                         said)
    # What runs: the Executor runs the Assistant's call, and the skill comes
    # with her voice brief in front.
    voice_profile.update_field(str(USER_ID), 'city', 'Madurai')
    brief = voice_profile.voice_brief(str(USER_ID))
    assert 'Madurai' in brief
    text = built.executor.function_map['reel_skill'](name=SKILL)
    assert not tool_reply_failed(text), text
    assert text == brief + '\n\n' + skills.get_skill_text(SKILL)
    assert built.assistant.function_map['reel_skill'](name='reel-edit') == text
