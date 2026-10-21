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

Her agent repeats this text to her, so it may promise only what reel_make
does.  What is checked here:

* its frontmatter says it is HARTOS's own text, not part of the MIT port;
* it reads the way the human-voice guide asks published text to read (the
  guide is the one style source): the guide's own checker reports nothing
  in it;
* every figure it gives (seconds, the steady beat's BPM, the on-screen
  line's words) sits in the sentence about the thing it measures, under the
  heading that says who decides it, and is the planner's own value for that
  thing: "hook_text for 4 s" fails although 4 s is the reveal's longest
  hold;
* everything it says her agent chooses is a reel_make argument.  Pace is
  not one: reel_make takes no pace, and the planner's pace comes from her
  agent record (style.agent_record_text: what she said when she made the
  agent), so a later "make this one calm" changes nothing, and the text
  says her agent cannot change it for one reel (review of 99888332f);
* what it says reel_make does is what the planner cuts: hard cuts only, no
  words on screen but her agent's hook_text, a reveal only when she filmed
  the finished look;
* it reaches her Reel Studio agent in CREATE and REUSE: the real
  constructors build her agent, reel_skill's schema names it to the model,
  and the Executor's reel_skill returns it with her voice brief in front.

    python -m pytest tests/unit/test_reel_edit_skill.py -q -p no:cacheprovider
"""
import inspect
import json
import os
import re

import pytest

from tests.unit.test_create_loop_end_to_end import USER_ID, create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_edl import _grid, _no_final_story, _story
from tests.unit.test_reel_tools import PID, _ok, _tools, _upload, env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, seats)

SKILL = 'reel_edit'
#: Its headings: what reel_make decides, what her agent chooses, what she films.
DOES, CHOOSE, FOOTAGE = 'What reel_make does', 'What you choose', 'Bridal footage'
#: A figure the text gives: a number or a range, then its unit.
_FIGURE = re.compile(r'(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?\s*(s|BPM|words)\b')
_N = r'(\d+(?:\.\d+)?)'
#: Her words on a later turn, asking for a calm reel (style.parse_style_hints
#: reads them as pace 'calm').
CALM_TURN = 'Make this one calm pace please, slow cuts'
#: Her agent record as CREATE saved it: nothing about pace.
HER_RECORD = {'goal': 'Bridal makeup reels from my clips, Tamil songs', 'flows': []}


def _skills():
    from integrations.reel_studio.igkit import skills
    return skills


def _text():
    return _skills().get_skill_text(SKILL)


def _frontmatter():
    from integrations.skills import registry
    path = os.path.join(os.path.dirname(_skills().__file__), SKILL, 'SKILL.md')
    with open(path, encoding='utf-8') as fh:
        meta, _body = registry._parse_frontmatter(fh.read())
    return meta


def _sections(text):
    """``{heading: body}`` of the text's '## ' sections ('' is the part
    before the first heading)."""
    parts = re.split(r'^## (.+)$', text, flags=re.M)
    out = {'': parts[0]}
    out.update(zip(parts[1::2], parts[2::2]))
    return out


def _reel_make_parameters():
    """reel_make's arguments, read off the tool her agent is given."""
    from integrations.reel_studio.tools import build_reel_tools
    return inspect.signature(dict(build_reel_tools('1', prompt_id='1'))['reel_make']).parameters


def test_it_is_one_of_the_skills_her_agent_can_read():
    skills = _skills()
    assert SKILL in skills.SKILL_NAMES
    assert SKILL in [s['name'] for s in skills.list_skills()]
    text = _text()
    assert text.startswith('# ' + SKILL)
    assert skills.get_skill_text(' Reel-Edit ') == text
    assert set(_sections(text)) == {'', DOES, CHOOSE, FOOTAGE}


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
    reports no invisible or typographic character, no stock word and no
    machine sentence shape in it."""
    from integrations.reel_studio.igkit.humanize import humanize_text
    text = _text()
    cleaned = humanize_text(text)
    assert cleaned['report'] == {'invisible': [], 'typographic': [], 'lexical': [],
                                 'structures': []}, cleaned['report']
    assert '\N{EM DASH}' not in text


# ── its figures ───────────────────────────────────────────────────────────

def _anchors():
    """``(heading, sentence, the planner's values)`` for every figure the
    text may give: the words around it name what it measures, the heading
    says who decides it."""
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.igkit.hooks import CARD_MAX_WORDS
    from integrations.reel_studio.media import beats, edl
    bar = C.QUALITY_BAR
    return (
        # The reveal: the planner's ideal hold and the quality bar's floor,
        # only when a final look was filmed (edl.plan_edit, no_final_look).
        (DOES, r'If she filmed the finished look, it ends on its best shot, held about %s-%s s '
               r'and never under %s s\. Without one there is no reveal' % (_N, _N, _N),
         (*edl.REVEAL_IDEAL_S, bar['reveal']['min_hold_s'])),
        (DOES, r'else a steady %s BPM' % _N, (beats.SYNTHETIC_BPM,)),
        # Pace: the planner's shot length for each pace, set from her agent
        # record; reel_make has no argument for it.
        (DOES, r'Pace is set by what she said when she made this agent: about %s s a shot, or '
               r'%s s if she asked for a calm pace\. You cannot change it for one reel' % (_N, _N),
         (edl.SHOT_S['beat'], edl.SHOT_S['calm'])),
        (CHOOSE, r'target_seconds: %s-%s s, %s s by default' % (_N, _N, _N),
         (*C.TARGET_S_RANGE, C.DEFAULT_TARGET_S)),
        (CHOOSE, r'under hook_text for %s s: at most %s words' % (_N, _N),
         (edl.HOOK_TEXT_S, CARD_MAX_WORDS)),
        (CHOOSE, r'open_with=final_look only when she asks to start with the result \(a teaser '
                 r'of %s s at most\)' % _N, (bar['teaser']['max_s'],)),
        # Filmed at least as long as the longest reveal, it is never slowed.
        (FOOTAGE, r'Hold the finished look at least %s s' % _N, (edl.REVEAL_IDEAL_S[1],)),
    )


def _misplaced(text):
    """What is wrong with the figures of ``text``: a sentence of _anchors
    missing (or twice) under its heading or giving another value, or a
    figure in no such sentence."""
    sections = _sections(text)
    problems, spans = [], {}
    for heading, pattern, want in _anchors():
        found = list(re.finditer(pattern, sections.get(heading, '')))
        if len(found) != 1:
            problems.append('%d x under %r: %s' % (len(found), heading, pattern))
            continue
        got = [float(g) for g in found[0].groups()]
        if got != [float(v) for v in want]:
            problems.append('%r gives %s, the planner %s' % (found[0].group(0), got, list(want)))
        spans.setdefault(heading, []).append(found[0].span())
    for heading, body in sections.items():
        for m in _FIGURE.finditer(body):
            if not any(a <= m.start() and m.end() <= b for a, b in spans.get(heading, ())):
                problems.append('%r under %r is in no sentence the planner backs'
                                % (m.group(0), heading or 'the top'))
    return problems


def test_every_figure_is_the_planners_own_for_what_it_measures():
    """Her agent repeats these to her; a figure the planner does not use for
    that thing would promise a reel reel_make does not make (the reveal's
    hold, the pace, the steady beat, the length, the on-screen line, the
    teaser, how long to film the finished look)."""
    assert _misplaced(_text()) == []


def test_the_figure_check_sees_a_figure_moved_changed_or_unknown():
    """Anti-vacuity for the check above."""
    text = _text()
    assert _misplaced(text.replace('hook_text for 2 s', 'hook_text for 4 s'))
    assert _misplaced(text.replace('a steady 120 BPM', 'a steady 90 BPM'))
    assert _misplaced(text + '\n- Each look takes 7 s to film.\n')
    # The pace sentence offered among her agent's choices (99888332f).
    pace = next(line for line in text.splitlines() if line.startswith('- Pace '))
    moved = text.replace(pace + '\n', '').replace(
        '\n\n## ' + FOOTAGE, '\n' + pace + '\n\n## ' + FOOTAGE)
    assert moved != text and _misplaced(moved)


# ── what her agent chooses, and what it does not ──────────────────────────

def _unbacked(choices, parameters):
    """The bullets of ``choices`` that name no reel_make argument."""
    return [line for line in choices.splitlines() if line.startswith('- ')
            and not any(re.search(r'\b%s\b' % re.escape(p), line) for p in parameters)]


def test_everything_it_says_her_agent_chooses_is_a_reel_make_argument():
    """A choice with no argument behind it is a promise her agent cannot
    keep (99888332f offered 'Pace follows her instructions', and reel_make
    takes no pace)."""
    parameters = list(_reel_make_parameters())
    choices = _sections(_text())[CHOOSE]
    assert re.search(r'^- ', choices, re.M)
    assert _unbacked(choices, parameters) == []
    assert _unbacked('- Pace follows her instructions: about 1.5 s a shot.', parameters)


@pytest.mark.parametrize('words', [CALM_TURN, 'calm'])
def test_no_reel_make_argument_changes_the_pace(env, words):
    """She asks for a calm reel on a later turn.  Whatever her agent passes
    to reel_make, the planner gets the pace her agent was made with, as the
    text tells her agent to say."""
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.style import parse_style_hints
    assert parse_style_hints(CALM_TURN) == {'pace': 'calm'}
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps(HER_RECORD), encoding='utf-8')
    _upload(env, b'clip' * 500)
    # Her words in every argument; clips stays '' (named clips are ids).
    args = {name: words for name in _reel_make_parameters() if name != 'clips'}
    _ok(_tools(env).run['reel_make'](**args))
    assert env.produced[-1]['style']['pace'] == C.STYLE_DEFAULTS['pace'] == 'beat'


def test_what_she_said_when_she_made_her_agent_sets_the_pace(env):
    (env.prompts / ('%s.json' % PID)).write_text(json.dumps(
        dict(HER_RECORD, extra_information='Calm pace, slow cuts')), encoding='utf-8')
    _upload(env, b'clip' * 500)
    _ok(_tools(env).run['reel_make']())
    assert env.produced[-1]['style']['pace'] == 'calm'


# ── what it says reel_make does ───────────────────────────────────────────

@pytest.mark.parametrize('pace', ['beat', 'calm'])
@pytest.mark.parametrize('story', [_story, _no_final_story], ids=['finished_look', 'no_final_look'])
def test_what_it_says_reel_make_does_is_what_the_planner_cuts(tmp_path, pace, story):
    """Hard cuts only, no fades or wipes; no words on screen but her agent's
    hook_text, from the start for edl.HOOK_TEXT_S (her logo is the only
    other overlay); a reveal only when she filmed the finished look.  When
    the planner gains another transition or overlay, this text changes with
    it."""
    from PIL import Image
    from integrations.reel_studio import contracts as C
    from integrations.reel_studio.media import edl as E
    logo = str(tmp_path / 'logo.png')
    Image.new('RGB', (400, 200), 'white').save(logo)
    analyses, moments = story()
    hook = 'Bare face to bride'
    plan = E.plan_edit(moments, analyses, _grid(), C.make_style(
        {'target_s': 20, 'pace': pace, 'hook_first': True, 'hook_text': hook}), logo_path=logo)
    assert {(s['transition']['type'], s['transition']['dur_s'])
            for s in plan['segments']} == {('cut', 0.0)}
    assert sorted(o['type'] for o in plan['overlays']) == ['logo', 'text']
    text = next(o for o in plan['overlays'] if o['type'] == 'text')
    assert (text['text'], text['from_s'], text['to_s']) == (hook, 0.0, E.HOOK_TEXT_S)
    roles = [s['role'] for s in plan['segments']]
    if story is _story:
        assert plan['final_look_found'] and roles[-1] == 'reveal'
    else:
        assert not plan['final_look_found'] and 'reveal' not in roles
        assert C.NOTE_NO_FINAL_LOOK in plan['notes']


# ── it reaches her agent ──────────────────────────────────────────────────

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
