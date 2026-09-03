"""_giveup_current_reuse_action -- closing the agent-8888 stale-ledger gap.

Root-caused 2026-08-28 (reconstructed from weeks of "agent 8888 has weird
pre-existing state" observations, e.g. the 08-06 [EMPTY-GROUPCHAT]
investigation and the 08-26 stale-agent-8888 collision that ate a whole
session's live-verification attempts): create_ledger_from_actions'
_find_resumable_session() reattaches to ANY ledger task whose status is
non-terminal, with no age/TTL check. The reuse loop in get_agent_response
gives up in three ways (turn/loop-deadline abort, budget exhaustion, an
empty groupchat) and in every case just returned/broke WITHOUT ever
marking the in-flight ledger task terminal -- so the abandoned task sat
PENDING/IN_PROGRESS forever, and the next unrelated message for that
(user, prompt) pair silently resumed the old stale goal instead of
starting fresh.

create_recipe.py's flow-complete path already has a sanctioned mechanism
for exactly this (#139's ActionState.GAVE_UP -> ledger FAILED, deliberately
re-openable for a retry) -- the reuse loop just never called it. This test
covers the extracted helper that now does, at all three give-up points.
"""
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

import reuse_recipe
from reuse_recipe import _giveup_current_reuse_action
from lifecycle_hooks import ActionState


class _FakeTask:
    def __init__(self, current_action):
        self.current_action = current_action


class TestGiveupCurrentReuseAction:
    def test_marks_current_action_gave_up(self, mock_flask_app):
        user_prompt = 'u1_8888'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(3)}), \
             patch('reuse_recipe.safe_set_state') as mock_set_state:
            _giveup_current_reuse_action(user_prompt, '[REUSE-LOOP-ABORT] bound hit: turn-deadline')

        mock_set_state.assert_called_once_with(
            user_prompt, 3, ActionState.GAVE_UP,
            '[REUSE-LOOP-ABORT] bound hit: turn-deadline')

    def test_unknown_user_prompt_is_a_safe_noop(self, mock_flask_app):
        with patch.object(reuse_recipe, 'user_tasks', {}), \
             patch('reuse_recipe.safe_set_state') as mock_set_state:
            _giveup_current_reuse_action('never_seen', 'reason')

        mock_set_state.assert_not_called()

    def test_never_raises_even_if_safe_set_state_blows_up(self, mock_flask_app):
        # This runs on a path that is already trying to deliver an honest
        # reply to the user (turn-deadline abort, budget exhaustion,
        # empty groupchat) -- it must never itself become the exception
        # that turns that reply into a raw traceback.
        user_prompt = 'u1_8888'
        with patch.object(reuse_recipe, 'user_tasks', {user_prompt: _FakeTask(1)}), \
             patch('reuse_recipe.safe_set_state', side_effect=RuntimeError('boom')):
            _giveup_current_reuse_action(user_prompt, 'reason')  # must not raise

    def test_each_giveup_site_calls_the_helper(self, mock_flask_app):
        # Cheap regression guard against the wiring being edited back out --
        # doesn't re-derive control flow, just confirms the call sites exist.
        import inspect
        src = inspect.getsource(reuse_recipe)
        assert src.count('_giveup_current_reuse_action(user_prompt, ') == 3
