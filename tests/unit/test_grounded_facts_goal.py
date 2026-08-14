"""
The grounded-facts marketing goal, checked for the wiring that orphans agents.

A seeded goal is text, and text can look perfectly correct while reaching no
tools at all. That has happened here before: register_news_tools was authored,
looked right, and was never loaded, because tool registration keys off
detect_goal_tags rather than off goal_type. Marketing goals also "completed"
for six weeks with no outreach side-effects for the same class of reason.

So these do not check that the description reads well. They check that the
description actually pulls in the tools it names, and that the grounding tool
is reachable from the agent rather than only from a Python import.
"""
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, __file__.rsplit('tests', 1)[0])

from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS  # noqa: E402
from integrations.agent_engine.marketing_tools import (  # noqa: E402
    detect_goal_tags, register_marketing_tools,
)

_SLUG = 'bootstrap_marketing_grounded_facts'


def _goal():
    for g in SEED_BOOTSTRAP_GOALS:
        if g['slug'] == _SLUG:
            return g
    raise AssertionError(f'{_SLUG} missing from SEED_BOOTSTRAP_GOALS')


class _Recorder:
    """Stands in for the autogen helper/assistant pair."""

    def __init__(self):
        self.names = []

    def register_for_llm(self, name=None, description=None):
        self.names.append(name)
        return lambda f: f

    def register_for_execution(self, name=None):
        return lambda f: f


class TestGoalReachesItsTools(unittest.TestCase):

    def test_the_description_triggers_marketing_tool_loading(self):
        """This is the orphaning check. Tools load from keywords in the text,
        not from goal_type, so a goal whose wording misses them gets an agent
        with no tools and produces prose instead of actions."""
        tags = detect_goal_tags(_goal()['description'])
        self.assertIn('marketing', tags)

    def test_verify_facts_is_registered_with_the_agent(self):
        """Reachable as a TOOL, not merely importable. A grounding function
        the agent cannot call is a grounding function that does not run."""
        helper = _Recorder()
        register_marketing_tools(helper, _Recorder(), 'u1')
        self.assertIn('verify_facts', helper.names)

    def test_every_tool_the_description_names_is_registered_or_global(self):
        helper = _Recorder()
        register_marketing_tools(helper, _Recorder(), 'u1')
        text = _goal()['description']
        for tool in ('verify_facts', 'post_to_channel'):
            self.assertIn(tool, text, f'{tool} not named in the goal text')
            self.assertIn(tool, helper.names, f'{tool} named but not registered')


class TestVerifyFactsTool(unittest.TestCase):
    """The tool wrapper, which is what the agent actually calls."""

    def _tool(self):
        captured = {}

        class _Grab(_Recorder):
            def register_for_execution(self, name=None):
                def deco(f):
                    captured[name] = f
                    return f
                return deco

        register_marketing_tools(_Recorder(), _Grab(), 'u1')
        return captured['verify_facts']

    def test_rejections_come_back_to_the_agent(self):
        """The agent must SEE what failed. A tool that returns only successes
        lets it publish the remainder believing all was well."""
        page = 'The octopus has nine brains and three hearts.'
        with patch('integrations.agent_engine.grounded_facts._fetch',
                   return_value=page):
            out = json.loads(self._tool()(json.dumps([
                {'claim': 'The octopus has nine brains.',
                 'source_url': 'https://example.org/o'},
                {'claim': 'The octopus has 14 brains.',
                 'source_url': 'https://example.org/o'},
            ])))
        self.assertTrue(out['success'])
        self.assertEqual(out['grounded_count'], 1)
        self.assertEqual(out['rejected_count'], 1)
        self.assertIn('14', out['rejected'][0]['reason'])

    def test_an_internal_error_does_not_read_as_all_clear(self):
        with patch('integrations.agent_engine.grounded_facts.verify_all',
                   side_effect=RuntimeError('boom')):
            out = json.loads(self._tool()('[{"claim":"x","source_url":"y"}]'))
        self.assertFalse(out['success'])
        self.assertEqual(out['grounded'], [])

    def test_malformed_input_fails_closed(self):
        out = json.loads(self._tool()('not json'))
        self.assertFalse(out['success'])
        self.assertEqual(out['grounded'], [])


class TestGoalShape(unittest.TestCase):

    def test_matches_the_shape_of_the_other_seeds(self):
        goal = _goal()
        reference = next(g for g in SEED_BOOTSTRAP_GOALS
                         if g['slug'] == 'bootstrap_news_regional')
        self.assertEqual(set(reference.keys()), set(goal.keys()))

    def test_slug_is_unique(self):
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        self.assertEqual(slugs.count(_SLUG), 1)

    def test_the_order_of_operations_survives_editing(self):
        """Grounding must come BEFORE composing. If a later edit reorders the
        steps so slides are made first, the gate becomes advisory."""
        text = _goal()['description']
        self.assertLess(text.index('verify_facts'), text.index('text_2_image'))
        self.assertLess(text.index('verify_facts'), text.index('post_to_channel'))


if __name__ == '__main__':
    unittest.main()
