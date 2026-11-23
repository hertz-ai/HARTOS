"""The plan writer is shown the tools the agent will have, and told that every
step names its way.

MEASURED 2026-11-04 on the owner's desktop: Reel Studio (agent 342) was built
through the INTERACTIVE plan writer (hartos/gather_agentdetails.py
AGENT_CREATOR_SYSTEM_MESSAGE), which is shown no tool at all and told to
"break down complex actions into multiple atomic steps".  Its approved plan
had 9 steps ("Extract the key visual elements and transitions from each clip",
"Plan the narrative arc", ...) that no tool does one at a time, while one
tool, reel_make, does the whole reel.  CREATE then stalled on step 2: the live
4B asked her for the clips it had just fetched (13 of 16 replays,
D:/hartos94_evidence/reel_1104/ab_results.md).

Owner 2026-11-04: "the agent should have realised these tools exists ...
the tool shd become a permanent member once the steps to get to final
outcome is known once"; "a tool could be a online github repo, a open source
software like ffmpeg ... shd find the recipe for reproducability"; "if there
is a will there is way should be the mantra for the agents".

These build the real plan-writer agents with autogen's classes replaced and
read the system message the model would be sent.

    python -m pytest tests/unit/test_the_plan_writer_knows_the_tools.py -q
"""
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from flask import Flask  # noqa: E402

from hartos import gather_agentdetails as ga  # noqa: E402

REEL = ['reel_studio']


def _system_message(**kwargs):
    with patch.object(ga.autogen, 'AssistantAgent') as agent_cls, \
            patch.object(ga.autogen, 'UserProxyAgent', return_value=MagicMock()), \
            patch('core.autogen_config.get_autogen_config_list',
                  return_value=[{'model': 'test', 'api_key': 'test'}]):
        ga.create_agents_for_user('10202', **kwargs)
    return agent_cls.call_args.kwargs['system_message']


class TestThePersonsPlanWriter:
    """The interactive writer, the one a person builds an agent with."""

    def test_it_is_shown_the_tools(self):
        message = _system_message()
        for name in ('get_chat_history', 'google_search', 'execute_coding_task'):
            assert name in message, name

    def test_a_reel_studio_agent_is_shown_its_own_tools(self):
        message = _system_message(goal_tags=REEL)
        assert 'reel_make' in message
        assert 'reel_trending_songs' in message

    def test_another_agent_is_not_shown_the_reel_tools(self):
        assert 'reel_make' not in _system_message()
        assert 'reel_make' not in _system_message(goal_tags=['marketing'])

    def test_every_step_names_its_way(self):
        message = _system_message()
        assert ga.PLAN_WAYS_RULE in message
        assert 'there is a will there is a way' in ga.PLAN_WAYS_RULE
        for way in ('ffmpeg', 'GitHub', 'google_search', 'execute_coding_task'):
            assert way in ga.PLAN_WAYS_RULE, way


class TestTheAutonomousPlanWriter:

    def test_a_reel_studio_goal_is_shown_its_own_tools(self):
        message = _system_message(autonomous=True, initial_description='make reels',
                                  goal_tags=REEL)
        assert 'reel_make' in message
        assert ga.PLAN_WAYS_RULE in message

    def test_without_the_tag_the_catalog_is_as_before(self):
        message = _system_message(autonomous=True, initial_description='make reels')
        assert ga.AUTONOMOUS_TOOL_CATALOG in message
        assert 'reel_make' not in message


def test_where_reel_studio_is_inert_its_tools_are_not_offered():
    """Central (amendment A8 d): the one tag rule drops reel_studio there, and
    the plan writer asks that rule, so it names no tool the agent will lack."""
    with patch('integrations.agent_engine.marketing_tools.resolve_goal_tags',
               return_value=[]):
        assert 'reel_make' not in _system_message(goal_tags=REEL)


def test_the_reel_lines_are_the_registered_descriptions():
    """One source: what the plan writer reads is what register_reel_studio_tools
    puts on the agent's schema (the first docstring line of each closure)."""
    from integrations.reel_studio import tools as reel_tools
    seen = {}

    class _Seat:
        def register_for_llm(self, name, description):
            seen[name] = description
            return lambda f: f

        def register_for_execution(self, name):
            return lambda f: f

    with patch.object(reel_tools, '_inert_here', return_value=False), \
            patch.object(reel_tools, '_first_registration'):
        reel_tools.register_reel_studio_tools(_Seat(), _Seat(), '10202', prompt_id='342',
                                              stored_tags=REEL)
    assert dict(reel_tools.describe_reel_tools()) == seen
    assert set(seen) == set(reel_tools.REEL_TOOL_NAMES)


def test_gather_info_hands_the_tags_to_the_writer():
    built = {}

    def _create(user_id, autonomous=False, initial_description=None, goal_tags=()):
        built['goal_tags'] = list(goal_tags)
        return MagicMock(), MagicMock()

    with Flask(__name__).app_context(), \
            patch.object(ga, 'user_agents', {}), \
            patch.object(ga, 'create_agents_for_user', side_effect=_create), \
            patch.object(ga, 'get_agent_response', return_value='{}'):
        ga.gather_info('10202', 'make me a reel agent', '342', goal_tags=REEL)
    assert built == {'goal_tags': REEL}


# -- a daemon goal turn's plan (central 2026-11-20, goal 5ba50f98) -----------
#
# The rice order desk's turn runs with the outreach CRM and journey tools
# (goal_manager.daemon_turn_stored_tags adds its goal type's tags), but its
# plan writer was shown neither family, so the plan
# (/app/data/data/prompts/89682852097.json) sent the outreach steps to
# execute_coding_task "because the dedicated outreach CRM tools are not in the
# available tool list", and the gate then asked for that tool's receipt.

#: The tags an outreach goal's turn is built with (its goal type's).
OUTREACH_TURN = ['crm', 'email', 'outreach', 'sales']
CRM_TOOLS = ('create_prospect', 'bulk_import_prospects', 'send_outreach_email',
             'create_followup_sequence', 'check_pending_followups',
             'move_prospect_stage', 'get_pipeline_status', 'list_sent_emails')
JOURNEY_TOOLS = ('view_journey_pipeline', 'advance_prospect_stage',
                 'run_journey_tick', 'send_prospect_message')


class TestAGoalTurnsPlanWriter:

    def test_an_outreach_goal_is_shown_its_crm_and_journey_tools(self):
        """FAILS BEFORE THE FIX: the writer listed no outreach tool."""
        message = _system_message(autonomous=True, initial_description='find rice buyers',
                                  goal_tags=OUTREACH_TURN)
        for name in CRM_TOOLS + JOURNEY_TOOLS:
            assert name in message, name
        assert ga.AUTONOMOUS_TOOL_CATALOG in message
        assert 'reel_make' not in message

    def test_without_the_tags_no_outreach_tool_is_offered(self):
        """A person's turn (no tags) and a Reel Studio agent: as before."""
        for message in (_system_message(autonomous=True, initial_description='plan my week'),
                        _system_message(), _system_message(goal_tags=REEL)):
            for name in CRM_TOOLS + JOURNEY_TOOLS:
                assert name not in message, name


def test_the_outreach_lines_are_the_registered_descriptions():
    """One source: each line the plan writer reads is a tool the family's
    own registration puts on the agent's schema, with its description's
    first paragraph (FAILS BEFORE THE FIX: none was listed)."""
    from integrations.agent_engine.journey_engine import register_journey_tools
    from integrations.agent_engine.outreach_crm_tools import register_outreach_tools
    seen = {}

    class _Seat:
        def register_for_llm(self, name=None, description=None, **_):
            def keep(f):
                seen[name] = ' '.join(description.strip().split('\n\n')[0].split())
                return f
            return keep

        def register_for_execution(self, name=None, **_):
            return lambda f: f

    register_outreach_tools(_Seat(), _Seat(), '10202')
    register_journey_tools(_Seat(), _Seat(), '10202')
    catalog = ga.family_tool_catalog(OUTREACH_TURN)
    assert set(seen) == set(CRM_TOOLS + JOURNEY_TOOLS)
    for name, first_line in seen.items():
        assert '  - %-22s %s\n' % (name, first_line) in catalog, name
