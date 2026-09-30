"""Guard: the REUSE opening turn is a dispatch the lifecycle parser can see.

Measured live 2026-09-25 on the installed Nunba (HARTOS 20bfca03d):
  [REUSE-VERIFY] action N ... no canonical receipt     42 lines, 14 sessions,
                                                        every one action 1
  "[REUSE] Action N TERMINATED, advancing"              0
POST /chat livetest_reuse_verify_1790351408 (prompt 88094979291) got 0
TERMINATED.

Root cause.  ``_reuse_seed_message`` built the opening turn as
``f"{message}\\n\\n{dispatch}"`` -- the user's words first, the
"Perform this action -> Action #1:" marker in the middle.  The one dispatch
parser, ``lifecycle_hooks.dispatch_action_id``, only honours a LEADING marker
(on purpose: text after a marker can quote an earlier marker).  So the seed was
not a dispatch, action 1 had no dispatch window, ``_reuse_completion_evidence``
found no receipt anywhere, ``_advance_or_steer`` forced GAVE_UP, and actions
2..N were never assigned.  Actions 2..N were unaffected because their dispatch
is the bare marker.

These tests drive the REAL seed builder and feed its output to the REAL
readers (the receipt finder and the verifier's window check).
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe  # noqa: E402

_UP = 'livetest_reuse_seed_user_1'
_TOOL = 'google_search'
_ACTION = f'Use {_TOOL} to find the population of Tokyo.'
_USER_TEXT = 'What is the population of Tokyo?'


class _FakeTask:
    current_action = 1
    evidence_seen_call_ids = set()

    def get_action(self, idx):
        return {'action': _ACTION}


class _FakeAgent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


_PROPOSAL = {'role': 'assistant', 'name': 'Helper', 'content': None,
             'tool_calls': [{'id': 'call_seed_1', 'type': 'function',
                             'function': {'name': _TOOL, 'arguments': '{}'}}]}
_RESULT = {'role': 'tool', 'name': 'Assistant',
           'content': 'Tokyo population: 14 million.',
           'tool_responses': [{'tool_call_id': 'call_seed_1', 'role': 'tool',
                               'content': 'Tokyo population: 14 million.'}]}
_VERDICT = {'role': 'user', 'name': 'StatusVerifier',
            'content': '{"status": "completed", "action_id": 1}'}


class ReuseSeedOpensActionOneWindow(unittest.TestCase):
    def setUp(self):
        self._saved_task = reuse_recipe.user_tasks.get(_UP)
        reuse_recipe.user_tasks[_UP] = _FakeTask()
        reuse_recipe.recipes[_UP] = {'actions': [{'recipe': [
            {'steps': 'search the web', 'tool_name': _TOOL}]}]}
        app = types.SimpleNamespace(logger=mock.MagicMock())
        self._app = mock.patch.object(reuse_recipe, 'current_app', app)
        self._app.start()
        self.seed = reuse_recipe._reuse_seed_message(_UP, _USER_TEXT)

    def tearDown(self):
        self._app.stop()
        if self._saved_task is None:
            reuse_recipe.user_tasks.pop(_UP, None)
        else:
            reuse_recipe.user_tasks[_UP] = self._saved_task
        try:
            del reuse_recipe.recipes[_UP]
        except Exception:
            pass

    def _chat(self):
        seed_msg = {'role': 'user', 'name': 'UserProxy', 'content': self.seed}
        return types.SimpleNamespace(
            messages=[seed_msg, _PROPOSAL, _RESULT, _VERDICT],
            agents=[_FakeAgent('Helper', [_TOOL]), _FakeAgent('Assistant', [])])

    def test_seed_is_parsed_as_the_action_1_dispatch(self):
        self.assertEqual(lh.dispatch_action_id(self.seed), 1,
                         f"the opening turn is not a dispatch: {self.seed!r}")

    def test_seed_still_carries_the_users_words(self):
        """ADDITIVE: the user's intent must still reach the group chat."""
        self.assertIn(_USER_TEXT, self.seed)
        self.assertTrue(reuse_recipe._reuse_is_pipeline_text(self.seed))

    def test_tool_result_after_the_seed_is_a_receipt(self):
        gc = self._chat()
        self.assertEqual(
            reuse_recipe._reuse_fabricated_tools(_UP, 1, gc, gc.agents), [],
            "precondition: the gate credits the tool run")
        self.assertEqual(
            reuse_recipe._reuse_completion_evidence(_UP, 1, gc),
            {'message_index': 2, 'kind': 'tool_receipt'},
            "action 1 passed the gate but its receipt is invisible -> GAVE_UP")

    def test_verifier_window_binds_the_receipt_to_action_1(self):
        gc = self._chat()
        with mock.patch.object(lh, 'get_registered_groupchat', lambda _u: gc):
            self.assertTrue(lh._verifier_completion_has_conversation_evidence(
                _UP, 1, {'evidence': {'message_index': 2,
                                      'kind': 'tool_receipt'}}))
            self.assertFalse(lh._verifier_completion_has_conversation_evidence(
                _UP, 2, {'evidence': {'message_index': 2,
                                      'kind': 'tool_receipt'}}))


class ReuseSeedSurvivesThePerMessageTokenCap(unittest.TestCase):
    """What reaches the MODEL, not what sits in group_chat.messages.

    Review of 9adccbcaf (measured on the banked recipes): every reuse context
    chain runs helper.token_limiter with AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
    and autogen's truncation keeps the HEAD of a message.  Dispatch-first put
    the steps (up to 1547 tokens for action 1 in the banked recipes) ahead of
    the user's words, so the cap cut the user's request off the wire.  The
    steps are the redundant part -- the recipe is also in the system prompt --
    so they must be what the cap eats.
    """

    def setUp(self):
        self._saved_task = reuse_recipe.user_tasks.get(_UP)
        reuse_recipe.user_tasks[_UP] = _FakeTask()
        # A long banked recipe: far more than one message's token cap.
        reuse_recipe.recipes[_UP] = {'actions': [{'recipe': [
            {'steps': f'step {i}: look up source {i} and note its figure',
             'tool_name': _TOOL,
             'generalized_functions': f'def f{i}(x):\n    return x * {i}\n'}
            for i in range(120)]}]}
        app = types.SimpleNamespace(logger=mock.MagicMock())
        self._app = mock.patch.object(reuse_recipe, 'current_app', app)
        self._app.start()
        self.seed = reuse_recipe._reuse_seed_message(_UP, _USER_TEXT)

    def tearDown(self):
        self._app.stop()
        if self._saved_task is None:
            reuse_recipe.user_tasks.pop(_UP, None)
        else:
            reuse_recipe.user_tasks[_UP] = self._saved_task
        reuse_recipe.recipes.pop(_UP, None)

    def _on_the_wire(self):
        from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
        from hartos import helper
        limiter = helper.token_limiter(
            max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
            max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
            min_tokens=0)
        out = limiter.apply_transform(
            [{'role': 'user', 'name': 'UserProxy', 'content': self.seed}])
        return out[0]['content']

    def test_precondition_the_seed_really_exceeds_the_cap(self):
        """Without this the test below could pass by never truncating."""
        self.assertNotEqual(self._on_the_wire(), self.seed)

    def test_the_users_words_survive_the_cap(self):
        self.assertIn(_USER_TEXT, self._on_the_wire())

    def test_the_truncated_seed_is_still_the_action_1_dispatch(self):
        self.assertEqual(lh.dispatch_action_id(self._on_the_wire()), 1)

    def test_actions_2_to_n_are_byte_identical(self):
        """The dispatch builder's other caller must not change at all."""
        msg = reuse_recipe._build_reuse_action_message(_UP, 1)
        self.assertTrue(msg.startswith(
            f"{reuse_recipe._REUSE_ACTION_MESSAGE_PREFIX}1:{_ACTION}"
            "\n follow these steps: "))
        self.assertNotIn(_USER_TEXT, msg)


if __name__ == '__main__':
    unittest.main()
