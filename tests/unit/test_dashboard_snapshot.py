"""Unit tests for DashboardService.get_agent_snapshot + get_agent_chat_tail.

Covers Phase B of the Agent Operations Console plan.  Mocks the
``AgentGoal`` table, ``_iter_ledgers`` walker, ``lifecycle_hooks`` cache,
and the audit log so the test runs without the full HARTOS bootstrap.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest


def _install_fake_models(monkeypatch, goal):
    """Inject a fake ``integrations.social.models`` module returning ``goal``."""
    fake_query = mock.MagicMock()
    fake_query.filter.return_value.first.return_value = goal
    fake_db = mock.MagicMock()
    fake_db.query.return_value = fake_query
    fake_db.close = mock.Mock()

    fake_models = SimpleNamespace(
        AgentGoal=type('AgentGoal', (), {'id': 'id'}),
        get_db=lambda: fake_db,
        CodingGoal=type('CodingGoal', (), {}),
        User=type('User', (), {}),
    )
    monkeypatch.setitem(sys.modules,
                        'integrations.social.models', fake_models)
    # The dashboard_service does `from .models import ...` — install
    # a relative-path alias too via the package's __dict__.
    #
    # THROUGH monkeypatch, so it is UNDONE. A raw `pkg.models = fake_models`
    # here was permanent: monkeypatch.setitem restores sys.modules, but a plain
    # attribute assignment on the real package object is never reverted, and
    # `from integrations.social import models` reads the package ATTRIBUTE in
    # preference to re-importing. So every later test in the same pytest
    # process got this SimpleNamespace instead of the real models module.
    #
    # That single line accounts for both dominant shapes in the shard-1 red
    # (50 failed across unrelated modules):
    #   - "'types.SimpleNamespace' object has no attribute '_uuid'" — the fake
    #     defines only AgentGoal/get_db/CodingGoal/User
    #   - "type object 'User' has no attribute '__table__'" — the fake's User is
    #     `type('User', (), {})`, a bare class with no SQLAlchemy mapping
    # which is why the failures looked like 50 unrelated bugs and were one.
    #
    # raising=False because the attribute may not exist yet on a fresh import;
    # monkeypatch then deletes it on teardown rather than restoring a value.
    pkg = sys.modules.get('integrations.social')
    if pkg is not None:
        monkeypatch.setattr(pkg, 'models', fake_models, raising=False)
    return fake_db


def _make_goal(goal_id='agent-1', status='active', last_dispatched_ago=None,
               owner_id='user-42', prompt_id='p-99'):
    last = None
    if last_dispatched_ago is not None:
        last = datetime.utcnow() - last_dispatched_ago
    return SimpleNamespace(
        id=goal_id,
        owner_id=owner_id,
        created_by=owner_id,
        goal_type='marketing',
        title='Test goal',
        description='desc',
        status=status,
        priority=10,
        spark_budget=200,
        spark_spent=50,
        prompt_id=prompt_id,
        last_dispatched_at=last,
    )


# ─── get_agent_snapshot ────────────────────────────────────────────────

def test_snapshot_returns_none_for_missing_agent(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=None)
    # Patch _iter_ledgers + audit so the snapshot's auxiliary lookups don't try real I/O
    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.api',
                        SimpleNamespace(_iter_ledgers=lambda **kw: iter([])))
    monkeypatch.setitem(sys.modules, 'agent_ledger',
                        SimpleNamespace(TaskStatus=SimpleNamespace(IN_PROGRESS='in_progress')))

    out = ds.DashboardService.get_agent_snapshot(fake_db, 'nope')
    assert out is None


def test_snapshot_marks_active_idle_when_never_dispatched(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='active', last_dispatched_ago=None)
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.api',
                        SimpleNamespace(_iter_ledgers=lambda **kw: iter([])))
    monkeypatch.setitem(sys.modules, 'agent_ledger',
                        SimpleNamespace(TaskStatus=SimpleNamespace(IN_PROGRESS='in_progress')))

    out = ds.DashboardService.get_agent_snapshot(fake_db, 'agent-1')
    assert out is not None
    assert out['agent']['status'] == 'idle'
    assert 'awaiting first tick' in (out['agent']['status_reason'] or '')


def test_snapshot_marks_stalled_when_old_dispatch(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='active',
                      last_dispatched_ago=timedelta(minutes=10))
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.api',
                        SimpleNamespace(_iter_ledgers=lambda **kw: iter([])))
    monkeypatch.setitem(sys.modules, 'agent_ledger',
                        SimpleNamespace(TaskStatus=SimpleNamespace(IN_PROGRESS='in_progress')))

    out = ds.DashboardService.get_agent_snapshot(fake_db, 'agent-1')
    assert out is not None
    assert out['agent']['status'] == 'stalled'
    assert 'No dispatch in' in (out['agent']['status_reason'] or '')


def test_snapshot_includes_tree_when_ledger_has_tasks(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal()
    fake_db = _install_fake_models(monkeypatch, goal=goal)

    task = SimpleNamespace(
        id='t1', title='Find prospects', status='in_progress',
        parent_task_id=None,
        created_at=datetime.utcnow() - timedelta(minutes=5),
        heartbeat_at=datetime.utcnow() - timedelta(minutes=1),
        blocked_reason=None,
    )
    # status.value access — make .value attribute too
    task.status = SimpleNamespace(value='in_progress')
    ledger = SimpleNamespace(tasks={'t1': task})

    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.api',
                        SimpleNamespace(_iter_ledgers=lambda **kw: iter([
                            ('agent-1', 'sess-A', ledger)
                        ])))
    monkeypatch.setitem(sys.modules, 'agent_ledger',
                        SimpleNamespace(TaskStatus=SimpleNamespace(IN_PROGRESS='in_progress')))

    out = ds.DashboardService.get_agent_snapshot(fake_db, 'agent-1')
    assert out is not None
    assert len(out['tree']) == 1
    assert out['tree'][0]['task_id'] == 't1'
    assert out['tree'][0]['status'] == 'in_progress'


# ─── get_agent_chat_tail ───────────────────────────────────────────────

def test_chat_tail_unregistered_returns_empty(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal()
    _install_fake_models(monkeypatch, goal=goal)

    # Stub lifecycle_hooks so the import succeeds but returns None.
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks',
                        SimpleNamespace(get_registered_groupchat=lambda key: None))

    out = ds.DashboardService.get_agent_chat_tail('agent-1', since_index=0, limit=10)
    assert out['registered'] is False
    assert out['messages'] == []
    assert out['next_index'] == 0


def test_chat_tail_returns_messages_after_cursor(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal()
    _install_fake_models(monkeypatch, goal=goal)

    fake_messages = [
        {'role': 'user', 'name': 'User', 'content': 'kick off'},
        {'role': 'assistant', 'name': 'Helper', 'content': 'on it'},
        {'role': 'assistant', 'name': 'Verify', 'content': 'looks good'},
    ]
    fake_gc = SimpleNamespace(messages=fake_messages)
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks',
                        SimpleNamespace(get_registered_groupchat=lambda key: fake_gc))

    out = ds.DashboardService.get_agent_chat_tail('agent-1', since_index=1, limit=10)
    assert out['registered'] is True
    assert len(out['messages']) == 2
    assert out['messages'][0]['speaker'] == 'Helper'
    assert out['messages'][1]['speaker'] == 'Verify'
    assert out['next_index'] == 3


def test_chat_tail_cold_fetch_caps_at_limit(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal()
    _install_fake_models(monkeypatch, goal=goal)

    fake_messages = [
        {'role': 'assistant', 'name': f'Agent{i}', 'content': f'turn {i}'}
        for i in range(100)
    ]
    fake_gc = SimpleNamespace(messages=fake_messages)
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks',
                        SimpleNamespace(get_registered_groupchat=lambda key: fake_gc))

    # Cold fetch with since=0 — should give us the LAST `limit` turns,
    # not the first; otherwise the drawer would render 100 historical
    # messages on every initial open.
    out = ds.DashboardService.get_agent_chat_tail('agent-1', since_index=0, limit=10)
    assert len(out['messages']) == 10
    assert out['messages'][0]['speaker'] == 'Agent90'
    assert out['messages'][-1]['speaker'] == 'Agent99'
    assert out['next_index'] == 100


# ─── _serialize_chat_message helper ────────────────────────────────────

def test_serialize_chat_message_handles_dict():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    msg = {'role': 'assistant', 'name': 'Helper', 'content': 'hi',
           'tool_calls': [{'name': 'web_search'}]}
    out = ds._serialize_chat_message(msg, 5)
    assert out['index'] == 5
    assert out['role'] == 'assistant'
    assert out['speaker'] == 'Helper'
    assert out['content'] == 'hi'
    assert out['tool_calls'] == [{'name': 'web_search'}]


def test_serialize_chat_message_handles_multipart_content():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    msg = {'role': 'user', 'content': [
        {'type': 'text', 'text': 'hello'},
        {'type': 'image_url', 'image_url': 'http://x'},
    ]}
    out = ds._serialize_chat_message(msg, 0)
    assert 'hello' in out['content']


def test_serialize_chat_message_falls_back_for_unknown_shape():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    out = ds._serialize_chat_message('plain string', 2)
    assert out['index'] == 2
    assert out['role'] == 'unknown'
    assert out['content'] == 'plain string'
