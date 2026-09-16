"""_advance_reuse_action — the empty-response bug.

Found 2026-08-19: when a REUSE recipe's last action completed,
_advance_reuse_action returned (None, False) for BOTH "all actions done"
and "genuine state error", so callers could not tell them apart.

A later, more thorough fix (#797/#798, 2026-09-09) closed the actual
symptom — the real answer being dropped as '' — at all six call sites in
_advance_or_steer by having them `break` to the loop's existing post-loop
extractor instead of returning early, so _advance_reuse_action itself only
needs the plain (next_id, advanced) contract; the caller doesn't need the
two failure shapes told apart.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

from hartos import reuse_recipe
from hartos.reuse_recipe import _advance_reuse_action
from hartos.lifecycle_hooks import ActionState


class _FakeTask:
    def __init__(self, n_actions):
        self.current_action = 1
        self.actions = [MagicMock() for _ in range(n_actions)]


class TestAdvanceReuseActionAllDone:
    def test_last_action_returns_all_done(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(1)}), \
             patch('hartos.reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('hartos.reuse_recipe.get_action_state', return_value=ActionState.TERMINATED):
            next_id, ok = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id is None
        assert ok is False

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
             patch('hartos.reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('hartos.reuse_recipe.get_action_state', return_value=ActionState.TERMINATED):
            _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert task.current_action == 1

    def test_state_error_returns_not_advanced(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(2)}), \
             patch('hartos.reuse_recipe.force_state_through_valid_path', return_value=False), \
             patch('hartos.reuse_recipe.get_action_state', return_value=ActionState.ERROR):
            next_id, ok = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id is None
        assert ok is False

    def test_mid_recipe_advances_normally(self, mock_flask_app):
        user_prompt = 'u1_p1'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(2)}), \
             patch('hartos.reuse_recipe.force_state_through_valid_path', return_value=True), \
             patch('hartos.reuse_recipe.safe_set_state', return_value=None):
            next_id, ok = _advance_reuse_action(user_prompt, 1, prompt_id='p1')

        assert next_id == 2
        assert ok is True
