"""The per-turn attach resolves a turn's words the way the agent was built:
through marketing_tools.resolve_goal_tags, with the agent record's STORED
goal tags (critic report item 2.6, amendment A8 b and d).

core.agent_tool_menu.attach_for_turn is the one per-turn attach, shared by
REUSE's get_agent_response and CREATE's _attach_for_create_turn.  It read
``detect_goal_tags(message)`` on its own, so the rules resolve_goal_tags
applies at construction did not hold for a single turn after it:

* her Reel Studio agent (reel_studio STORED on its record, on her desktop)
  has media, marketing and outreach quieted unless her words really ask for
  them (A8 b) -- yet a turn saying "find a trending Tamil song for this
  reel" unlocked media mid-chat and attached the music and TTS tools the
  build had kept off her schema;
* on the central node reel_studio is inert (A8 d) -- yet a turn saying
  "edit my clips" unlocked it there.

The builders now record the record's stored tags on the attach ledger
(arm_turn_attach, its one writer), and attach_for_turn resolves each turn
against them -- the same one rule, no second gate in the constructors
(test_hierarchical_tool_gate.py still counts one resolution per
constructor).  Detection alone still quiets nothing: an agent whose words
merely mention editing her videos is not her agent (A8 b).

The first half drives the real attach_for_turn with recording agents and a
two-tool registry; the second builds her agent with each pipeline's real
constructor (the ``seats`` harness of test_reel_tools_wired_both_pipelines.py)
and runs the attach on what was built.

    python -m pytest tests/unit/test_turn_attach_reads_stored_tags.py -q -p no:cacheprovider
"""
from types import SimpleNamespace

import pytest

from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, seats)

#: A turn of hers that trips the media detector ('song') without asking to
#: compose anything.
SONG_TURN = 'Find a trending Tamil song for this reel'
#: A turn that trips the marketing detector ('brand', 'engagement').
BRAND_TURN = 'Boost engagement on my brand page with this reel'
#: A turn that really asks for the media family (a _REEL_STUDIO_KEEPS phrase).
JINGLE_TURN = 'Compose a jingle for my salon intro'


class _Agent:
    """Records the registrations attach_for_tags makes through register_dual."""

    def __init__(self, name):
        self.name = name
        self.llm, self.exe = set(), set()

    def register_for_llm(self, name=None, description=None):
        self.llm.add(name)
        return lambda f: f

    def register_for_execution(self, name=None):
        self.exe.add(name)
        return lambda f: f


def _registry():
    """A media tool (tts/speech, unlocked by 'media') and a web tool
    (web/scraping, unlocked by 'marketing', 'outreach' and 'news')."""
    tools = {
        'pocket_tts': SimpleNamespace(
            tags=['tts', 'speech'], description='offline speech synthesis',
            endpoints={'synthesize': {'description': 'Text to speech'}}),
        'crawl4ai': SimpleNamespace(
            tags=['web', 'scraping'], description='web crawler',
            endpoints={'crawl': {'description': 'Crawl a webpage'}}),
    }
    return SimpleNamespace(_tools=tools,
                           create_endpoint_function=lambda t, e: (lambda **kw: 'ok'))


@pytest.fixture
def desktop(monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    return monkeypatch


def _built(stored, goal):
    """A helper/executor pair armed the way both builders arm it: the tags
    resolved for the build, and the record's stored tags."""
    from core.agent_tool_menu import arm_turn_attach
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    helper, executor = _Agent('Helper'), _Agent('Assistant')
    arm_turn_attach(executor, set(), resolve_goal_tags(stored, goal), stored)
    return helper, executor


def _turn(helper, executor, text):
    from core.agent_tool_menu import attach_for_turn
    return attach_for_turn(text, helper, executor, _registry())


# ── her Reel Studio agent, on her desktop ─────────────────────────────────

def test_her_agent_keeps_media_quiet_when_a_turn_names_a_song(desktop):
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    assert 'media' in detect_goal_tags(SONG_TURN), 'the premise: the turn trips media'
    helper, executor = _built(['reel_studio'], HER_GOAL)
    new, n = _turn(helper, executor, SONG_TURN)
    assert 'media' not in new and n == 0, (
        f'her stored reel_studio quiets media for the build; a turn naming a song '
        f'unlocked it again mid-chat: +{new} -> {n} tools')
    assert 'media' not in executor._hart_unlocked_tags
    assert 'pocket_tts_synthesize' not in helper.llm | executor.exe


def test_her_agent_keeps_marketing_quiet_when_a_turn_names_her_brand(desktop):
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    assert 'marketing' in detect_goal_tags(BRAND_TURN), 'the premise'
    helper, executor = _built(['reel_studio'], HER_GOAL)
    new, n = _turn(helper, executor, BRAND_TURN)
    assert 'marketing' not in new and n == 0, (new, n)
    assert 'crawl4ai_crawl' not in helper.llm


def test_her_words_that_really_ask_for_the_family_still_unlock_it(desktop):
    helper, executor = _built(['reel_studio'], HER_GOAL)
    new, n = _turn(helper, executor, JINGLE_TURN)
    assert 'media' in new and n == 1, (new, n)
    assert 'pocket_tts_synthesize' in helper.llm
    assert 'pocket_tts_synthesize' in executor.exe
    # Idempotent: the next turn unlocks nothing more.
    assert _turn(helper, executor, JINGLE_TURN) == ([], 0)


# ── every other agent: unchanged ──────────────────────────────────────────

def test_any_other_agent_still_gets_media_from_a_song_turn(desktop):
    """The suppression is her stored agent's only: the same turn on an agent
    whose record stores no reel_studio unlocks media, as before."""
    helper, executor = _built(None, 'Summarise three sources on local AI costs')
    new, n = _turn(helper, executor, SONG_TURN)
    assert 'media' in new and n == 1, (new, n)
    assert 'pocket_tts_synthesize' in helper.llm


def test_detection_alone_quiets_nothing(desktop):
    """Amendment A8 b: a turn whose words detect reel_studio, on an agent
    that does not store it, unlocks the tag and keeps media open."""
    helper, executor = _built(None, 'Summarise three sources on local AI costs')
    new, _n = _turn(helper, executor, 'Edit my clips and find a trending Tamil song')
    assert 'reel_studio' in new and 'media' in new, new


def test_a_builder_that_records_no_stored_tags_resolves_as_detection(desktop):
    """arm_turn_attach's stored tags default to none: a ledger armed with
    the three arguments (the vote harness, the ctx-fit harness) resolves a
    turn exactly as the detector reads it."""
    from core.agent_tool_menu import arm_turn_attach, attach_for_turn
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    helper, executor = _Agent('Helper'), _Agent('Assistant')
    arm_turn_attach(executor, set(), [])
    new, _n = attach_for_turn(SONG_TURN, helper, executor, _registry())
    assert new == detect_goal_tags(SONG_TURN)


# ── the central node: reel_studio is inert ────────────────────────────────

def test_on_central_a_turn_never_unlocks_reel_studio(monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    for stored in (['reel_studio'], None):
        helper, executor = _built(stored, HER_GOAL)
        new, _n = _turn(helper, executor, 'Please edit my clips for Instagram')
        assert 'reel_studio' not in new, (stored, new)
        assert 'reel_studio' not in executor._hart_unlocked_tags


def test_on_central_a_stored_reel_studio_quiets_nothing(monkeypatch):
    """Inert means inert: central's own goals keep every family, so the
    stored tag does not quiet media there either."""
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    helper, executor = _built(['reel_studio'], HER_GOAL)
    new, n = _turn(helper, executor, SONG_TURN)
    assert 'media' in new and n == 1, (new, n)


# ── both pipelines' real constructors hand the attach her stored tags ─────

@pytest.fixture
def roomy(monkeypatch):
    """A window the attach fits into (and no /props probe of a live server)."""
    from core import llm_outbound_logger as wire
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: 1 << 17)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_her_agent_built_by_either_pipeline_keeps_media_quiet_mid_chat(seats, roomy, pipeline):
    from core.agent_tool_menu import attach_for_turn
    from integrations.service_tools import service_tool_registry
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    assert built.assistant._hart_unlocked_tags == {'reel_studio'}
    new, n = attach_for_turn(SONG_TURN, built.helper, built.assistant,
                             service_tool_registry)
    assert 'media' not in new, (
        f'{pipeline}: the constructor armed the turn attach without her stored '
        f'tags, so a song turn unlocked media: +{new} -> {n} tools')
    assert 'media' not in built.assistant._hart_unlocked_tags


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_any_other_agent_built_by_either_pipeline_still_unlocks_media(seats, roomy, pipeline):
    """Anti-vacuity for the test above: the same turn, the same constructor,
    a record that stores no reel_studio."""
    from core.agent_tool_menu import attach_for_turn
    from integrations.service_tools import service_tool_registry
    built = seats(pipeline)
    new, _n = attach_for_turn(SONG_TURN, built.helper, built.assistant,
                              service_tool_registry)
    assert 'media' in new, (pipeline, new)
