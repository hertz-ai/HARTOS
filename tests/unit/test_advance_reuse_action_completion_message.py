"""_advance_reuse_action / _finish_reuse_recipe — the empty-response bug.

Found 2026-08-19: when a REUSE recipe's last action completed,
_advance_reuse_action returned (None, False) for BOTH "all actions done"
and "genuine state error". Callers treated both as failure and did
`return ''`, discarding the StatusVerifier's real completion message —
e.g. a pure-arithmetic task computed the right answer and StatusVerifier
confirmed it, but the HTTP response body was "".

Fix: _advance_reuse_action now returns a third element distinguishing
the two cases, and callers deliver the completion message via
_finish_reuse_recipe instead of dropping it.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

import reuse_recipe
from reuse_recipe import _advance_reuse_action, _finish_reuse_recipe
from lifecycle_hooks import ActionState


class _FakeTask:
    def __init__(self, n_actions):
        self.current_action = 1
        self.actions = [MagicMock() for _ in range(n_actions)]


class TestAdvanceReuseActionAllDone:
    def test_last_action_returns_all_done_true(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(1)}), \
             patch('reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('reuse_recipe.get_action_state', return_value=ActionState.TERMINATED):
            next_id, ok, done = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id is None
        assert ok is False
        assert done is True

    def test_all_done_resets_current_action_for_next_turn(self, mock_flask_app):
        """Found live 2026-08-31: on the "all actions done" path,
        current_action was left at next_id (out of range) permanently.
        user_tasks[user_prompt] is reused across every later message from
        the same identity (create_agents_for_user's fresh Action() only
        runs on that identity's first-ever turn), so every subsequent
        message immediately hit "current_action > len(actions)", burned
        its retries doing nothing, and fell through to echoing the user's
        own prompt back instead of a real reply. Must reset to 1 so the
        next turn (which re-runs the same recipe) starts sanely."""
        user_prompt = 'u1_p1'
        task = _FakeTask(1)
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: task}), \
             patch('reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('reuse_recipe.get_action_state', return_value=ActionState.TERMINATED):
            _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert task.current_action == 1

    def test_state_error_returns_all_done_false(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(2)}), \
             patch('reuse_recipe.force_state_through_valid_path', return_value=False), \
             patch('reuse_recipe.get_action_state', return_value=ActionState.ERROR):
            next_id, ok, done = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id is None
        assert ok is False
        assert done is False

    def test_mid_recipe_advances_normally(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(2)}), \
             patch('reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('reuse_recipe.safe_set_state', return_value=None):
            next_id, ok, done = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id == 2
        assert ok is True
        assert done is False


class TestFinishReuseRecipeDeliversMessage:
    def test_delivers_and_returns_the_completion_message(self, mock_flask_app):
        json_obj = {"status": "completed", "action_id": 1, "message": "8347 * 962 = 8029814"}
        with patch('reuse_recipe.send_message_to_user1') as mock_send:
            result = _finish_reuse_recipe('u1', 'p1', json_obj)

        assert result == "8347 * 962 = 8029814"
        mock_send.assert_called_once_with('u1', "8347 * 962 = 8029814", '', 'p1')

    def test_no_message_key_returns_empty_and_sends_nothing(self, mock_flask_app):
        json_obj = {"status": "completed", "action_id": 1}
        with patch('reuse_recipe.send_message_to_user1') as mock_send:
            result = _finish_reuse_recipe('u1', 'p1', json_obj)

        assert result == ''
        mock_send.assert_not_called()

    def test_non_dict_json_obj_is_safe(self, mock_flask_app):
        with patch('reuse_recipe.send_message_to_user1') as mock_send:
            result = _finish_reuse_recipe('u1', 'p1', None)

        assert result == ''
        mock_send.assert_not_called()
