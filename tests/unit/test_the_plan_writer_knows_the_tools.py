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
