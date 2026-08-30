"""Behavioural tests: the steering bridge can target a running flywheel goal
(2026-05-31).

ROOT: dashboard_service.inject_instruction keyed the live-GroupChat lookup on
AgentGoal.prompt_id, which is NULL for autonomous flywheel goals (dispatch_goal
generates a deterministic goal_id->prompt_id hash but never writes it back).
So the Claude co-pilot's inject bridge returned "agent not found or has no
prompt_id" and could never steer a flywheel goal — exactly the goals we want
co-piloted (e.g. to emit the recipe JSON the local model can't produce).

FIX: inject_instruction recomputes the SAME deterministic prompt_id
(dispatch.prompt_id_for_goal — single source) and tries it against the owner
candidates the daemon may have dispatched under, so a running flywheel goal's
GroupChat resolves and the operator/co-pilot message lands in it.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine.dispatch import prompt_id_for_goal  # noqa: E402
from integrations.social import dashboard_service as ds  # noqa: E402


def _db_returning(goal):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = goal
    return db


class _GC:
    def __init__(self):
        self.messages = []


def test_prompt_id_for_goal_is_deterministic_and_numeric():
    gid = 'b6b8be8d-b70e-4e9a-be0f-2aa35bcde45b'
    a = prompt_id_for_goal(gid)
    b = prompt_id_for_goal(gid)
    assert a == b, "same goal_id must map to the same prompt_id"
    assert a.isdigit(), "prompt_id must be numeric (isdigit gate in /chat)"
    assert prompt_id_for_goal('other') != a, "different goals differ"


def test_source_guard_agent_daemon_uses_single_source_prompt_id_hash():
    """DRY guard (paired with the behavioural determinism test above): the daemon
    must store goal.prompt_id via dispatch.prompt_id_for_goal, NOT a second copy
    of the md5(goal.id) formula.  A duplicate passes today (identical values) but
    silently drifts the day prompt_id_for_goal changes — breaking REUSE tracking
    AND the bridge's {owner}_{hash} GroupChat lookup, which no single-call-site
    behavioural test can catch (the values are equal until the formula moves)."""
    import inspect
    from integrations.agent_engine import agent_daemon
    src = inspect.getsource(agent_daemon)
    assert 'prompt_id_for_goal' in src, (
        "agent_daemon must resolve prompt_id via the single-source helper")
    assert 'hashlib.md5(str(goal.id)' not in src, (
        "agent_daemon has a duplicate md5(goal.id) prompt_id hash — it must call "
        "dispatch.prompt_id_for_goal so the value matches the recipe path + bridge")


def test_inject_resolves_flywheel_goal_with_null_prompt_id():
    """The fix: a flywheel goal (prompt_id=None) whose live GroupChat is
    registered under {owner}_{hash} must now resolve and accept the inject."""
    goal_id = 'b6b8be8d-b70e-4e9a-be0f-2aa35bcde45b'
    goal = SimpleNamespace(prompt_id=None, owner_id='owner42',
                           created_by='owner42', user_id=None)
    gc = _GC()
    key = f'owner42_{prompt_id_for_goal(goal_id)}'

    def _resolver(k):
        return gc if k == key else None

    with patch('hartos.lifecycle_hooks.get_registered_groupchat', side_effect=_resolver), \
         patch('security.immutable_audit_log.get_audit_log'):
        out = ds.inject_instruction(_db_returning(goal), goal_id,
                                    'Emit the recipe JSON now.', actor_id='claude-copilot')

    assert out['ok'] is True, out
    assert out['message_index'] == 0
    assert gc.messages and gc.messages[-1]['content'] == 'Emit the recipe JSON now.'
    assert 'claude-copilot' in gc.messages[-1]['name']


def test_inject_resolves_flywheel_goal_under_system_user():
    """Daemon may dispatch under 'system' when the goal has no owner — the
    bridge must try that candidate too."""
    goal_id = 'deadbeef-0000'
    goal = SimpleNamespace(prompt_id=None, owner_id=None, created_by=None,
                           user_id=None)
    gc = _GC()
    key = f'system_{prompt_id_for_goal(goal_id)}'
    with patch('hartos.lifecycle_hooks.get_registered_groupchat',
               side_effect=lambda k: gc if k == key else None), \
         patch('security.immutable_audit_log.get_audit_log'):
        out = ds.inject_instruction(_db_returning(goal), goal_id, 'steer', actor_id='cp')
    assert out['ok'] is True, out


def test_inject_still_resolves_human_goal_via_row_prompt_id():
    """Regression: a human/coding agent with a real prompt_id on the row must
    still resolve under {owner}_{prompt_id} (unchanged behaviour)."""
    goal = SimpleNamespace(prompt_id='12345', owner_id='u1',
                           created_by='u1', user_id=None)
    gc = _GC()
    with patch('hartos.lifecycle_hooks.get_registered_groupchat',
               side_effect=lambda k: gc if k == 'u1_12345' else None), \
         patch('security.immutable_audit_log.get_audit_log'):
        out = ds.inject_instruction(_db_returning(goal), 'agent-x', 'hello', actor_id='admin')
    assert out['ok'] is True, out
    assert gc.messages[-1]['content'] == 'hello'


def test_inject_no_live_groupchat_returns_clear_error():
    """No registered GroupChat (goal not currently executing) → ok=False with
    a clear, non-crashing message."""
    goal = SimpleNamespace(prompt_id=None, owner_id='o', created_by='o', user_id=None)
    with patch('hartos.lifecycle_hooks.get_registered_groupchat', return_value=None), \
         patch('security.immutable_audit_log.get_audit_log'):
        out = ds.inject_instruction(_db_returning(goal), 'gid', 'x', actor_id='cp')
    assert out['ok'] is False
    assert 'no live GroupChat' in (out['error'] or '')


def test_inject_empty_instruction_rejected():
    out = ds.inject_instruction(MagicMock(), 'gid', '   ', actor_id='cp')
    assert out['ok'] is False
    assert 'empty' in (out['error'] or '').lower()
