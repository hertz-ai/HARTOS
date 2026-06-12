"""Behavioural test for the deterministic recipe-request speaker routing.

create_recipe.state_transition pins the StatusVerifier for recipe-creation
requests instead of letting the LLM speaker-selector hand them to the Assistant
(which echoes the prompt / replies "I'm not sure I understand" — live evidence
2026-06-07 19:13-19:18: one action thrashed ~5 min through dozens of "could NOT
be parsed" retries before a chance StatusVerifier turn finally emitted the
recipe JSON and the action completed instantly).

The routing decision keys off the pure predicate
``lifecycle_hooks.is_recipe_creation_request``, exercised directly here.
state_transition itself can't be unit-tested in this env — it's a closure
inside create_recipe, which imports autogen (absent in the unit-test install);
lifecycle_hooks is autogen-free, which is exactly why the predicate lives
there.  The builders are bound to ``RECIPE_CREATE_PROMPT_PREFIX`` by
construction, so the predicate provably matches what request_recipe_for_action
emits.
"""
from __future__ import annotations

import unittest

from lifecycle_hooks import (
    is_recipe_creation_request,
    RECIPE_CREATE_PROMPT_PREFIX,
)


class IsRecipeCreationRequestTest(unittest.TestCase):

    def test_matches_request_recipe_for_action_shape(self):
        # The exact shape request_recipe_for_action emits (prefix + body).
        prompt = (RECIPE_CREATE_PROMPT_PREFIX +
                  ' that includes only the necessary steps for this action, '
                  'along with a suitable name. Provide the output in the '
                  'following JSON format: { "status": "done", ... }')
        self.assertTrue(is_recipe_creation_request(prompt))

    def test_matches_last_action_variant_shape(self):
        # request_recipe_for_action_last uses "...for this action from history".
        prompt = (RECIPE_CREATE_PROMPT_PREFIX +
                  ' that includes only the necessary steps for this action '
                  'from history, along with a suitable name.')
        self.assertTrue(is_recipe_creation_request(prompt))

    def test_matches_when_echoed_mid_message(self):
        # An agent that echoes the prompt must still be detected (route it to
        # StatusVerifier rather than loop) — hence `in`, not just startswith.
        echoed = ('Sure, I will ' + RECIPE_CREATE_PROMPT_PREFIX +
                  ' for this action right away.')
        self.assertTrue(is_recipe_creation_request(echoed))

    def test_rejects_action_execution_message(self):
        # "Execute Action N" must keep the normal Assistant→verify status flow.
        self.assertFalse(is_recipe_creation_request(
            'Execute Action 1: do the thing ,Latest User message: hi'))

    def test_rejects_scheduler_prompt(self):
        # begin_agent_convo_to_get_schedulers uses a different ("Reflect ...")
        # prompt that must NOT be pinned to StatusVerifier.
        self.assertFalse(is_recipe_creation_request(
            'Reflect on the sequence of tasks and create scheduled_tasks with '
            'proper persona name and action_entry_point.'))

    def test_rejects_status_done_json_reply(self):
        # The StatusVerifier's actual recipe JSON reply is the RESPONSE, not a
        # request — it must flow into the JSON-parse path, never re-route.
        self.assertFalse(is_recipe_creation_request(
            '{\n  "status": "done",\n  "action": "x",\n  "recipe": [] }'))

    def test_empty_none_and_non_str_are_false(self):
        self.assertFalse(is_recipe_creation_request(''))
        self.assertFalse(is_recipe_creation_request(None))
        self.assertFalse(is_recipe_creation_request(123))
        self.assertFalse(is_recipe_creation_request({'content': 'x'}))


if __name__ == '__main__':
    unittest.main()
