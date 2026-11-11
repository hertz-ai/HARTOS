"""A reuse turn sees what the person's camera showed, without the model having
to think of calling the camera tool.

MEASURED live 2026-11-11 on build 69 (00:14-00:47 IST, Syllable Coach
1793359802508, a test child account): the caption rows were right ("A simple
stick figure pushes a red cart", "A brown square sits on a beige floor"), and
the agent's turns never looked: the child turning to a toy car and the chair
going empty changed nothing.  A reuse turn's opening message carried the
person's words and the action only; the canonical context builder
(core.user_context through reuse_recipe.get_action_user_details) renders a
tagged five-minute camera window and had no caller in reuse_recipe
(claude-autostart-29's read of fffdffc96).

Now the opening message carries that window, between the person's words and
the steps (the token cap keeps a message's head, so the steps go first), read
for the turn's own account only.  With no camera captions the message is as
before.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hartos import reuse_recipe  # noqa: E402

_UP = 'child-1_4242'
_ACTION = 'Ask the child to say "ball" and wait for the attempt.'
_WORDS = 'car! vroom vroom... ba'
_WINDOW = ('<Last_5_Minutes_Visual_Context_Start>, '
           'A simple stick figure pushes a red cart on 2026-11-11T00:19:47, '
           '<Last_5_Minutes_Visual_Context_End>')
_ACTIONS_WITH_WINDOW = (f'opened the app on 2026-11-11T00:14:00, {_WINDOW}, '
                        "If a person is identified in Visual_Context section that's most "
                        'probably the user (me) & most likely not taking any selfie., '
                        '<Last_2_Minutes_Screen_Context_Start>, a code editor on '
                        '2026-11-11T00:19:50, <Last_2_Minutes_Screen_Context_End>')


class _FakeTask:
    current_action = 1
    evidence_seen_call_ids = set()

    def get_action(self, idx):
        return {'action': _ACTION}


class TestTheTurnSeesTheCamera(unittest.TestCase):

    def setUp(self):
        self._saved_task = reuse_recipe.user_tasks.get(_UP)
        self._saved_recipe = reuse_recipe.recipes.get(_UP)
        reuse_recipe.user_tasks[_UP] = _FakeTask()
        reuse_recipe.recipes[_UP] = {'actions': [{'recipe': [
            {'steps': 'send the child a prompt', 'tool_name': 'send_message_to_user'}]}]}
        self._app = mock.patch.object(reuse_recipe, 'current_app',
                                      types.SimpleNamespace(logger=mock.MagicMock()))
        self._app.start()

    def tearDown(self):
        self._app.stop()
        for store, saved in ((reuse_recipe.user_tasks, self._saved_task),
                             (reuse_recipe.recipes, self._saved_recipe)):
            if saved is None:
                store.pop(_UP, None)
            else:
                store[_UP] = saved

    def _seed(self, details):
        with mock.patch.object(reuse_recipe, 'get_action_user_details', details):
            return reuse_recipe._reuse_seed_message(_UP, _WORDS)

    def _plain(self):
        return reuse_recipe._build_reuse_action_message(_UP, 1, user_words=_WORDS)

    def test_the_camera_window_rides_with_the_words(self):
        seed = self._seed(mock.MagicMock(return_value=('details', _ACTIONS_WITH_WINDOW)))
        self.assertIn(_WINDOW, seed)
        from core.constants import ACTION_STEPS_SEPARATOR
        head = seed.split(ACTION_STEPS_SEPARATOR)[0]
        self.assertIn(_WORDS, head)
        self.assertIn(_WINDOW, head, 'the window sits before the steps, where the cap keeps it')
        self.assertLess(head.index(_WORDS), head.index(_WINDOW))

    def test_only_the_camera_window_is_taken(self):
        seed = self._seed(mock.MagicMock(return_value=('details', _ACTIONS_WITH_WINDOW)))
        self.assertNotIn('a code editor', seed, "the owner's screen is not this turn's camera")
        self.assertNotIn('opened the app', seed)
        self.assertNotIn('details', seed.split(_ACTION)[0])

    def test_the_window_is_read_for_the_turns_own_account(self):
        details = mock.MagicMock(return_value=('details', _ACTIONS_WITH_WINDOW))
        self._seed(details)
        details.assert_called_once_with('child-1')

    def test_with_no_camera_captions_the_message_is_as_before(self):
        seed = self._seed(mock.MagicMock(return_value=('details', 'user has not performed any actions yet.')))
        self.assertEqual(seed, self._plain())

    def test_a_window_that_cannot_be_read_leaves_the_message_as_before(self):
        seed = self._seed(mock.MagicMock(side_effect=RuntimeError('store down')))
        self.assertEqual(seed, self._plain())
