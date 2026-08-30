"""Unit tests for Phase C + D: A2A graph + steering + inject + ETA.

Covers ``dashboard_service.get_a2a_graph``, ``steer_agent``,
``inject_instruction``, and ``_compute_eta_from_tree``.  Mocks
``internal_agent_communication`` singletons, ``AgentGoal``, the
ledger walker, and the audit log so tests run without HARTOS bootstrap.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest


# ─── Fake module helpers ──────────────────────────────────────────────


def _install_fake_audit(monkeypatch):
    """Inject fake security.immutable_audit_log so writes don't blow up.

    Returns the captured-events list — tests can assert on it.
    """
    captured = []

    class _Audit:
        def log_event(self, **kwargs):
            captured.append(kwargs)

    audit = _Audit()

    fake_security = SimpleNamespace()
    fake_module = SimpleNamespace(get_audit_log=lambda: audit)
    monkeypatch.setitem(sys.modules, 'security', fake_security)
    monkeypatch.setitem(sys.modules,
                        'security.immutable_audit_log', fake_module)
    return captured


def _install_fake_iac(monkeypatch, delegations):
    """Inject internal_agent_communication with ``a2a_context.delegations``.

    ``from integrations.internal_comm import internal_agent_communication``
    triggers an attribute lookup on the parent package — we must set the
    submodule as an attribute on the fake parent, otherwise import yields
    ``ImportError`` and the production code falls through to empty.
    """
    a2a_ctx = SimpleNamespace(delegations=delegations)
    fake_iac = SimpleNamespace(a2a_context=a2a_ctx)
    fake_pkg = SimpleNamespace(internal_agent_communication=fake_iac)
    monkeypatch.setitem(sys.modules, 'integrations.internal_comm', fake_pkg)
    monkeypatch.setitem(sys.modules,
                        'integrations.internal_comm.internal_agent_communication',
                        fake_iac)


def _install_fake_models(monkeypatch, goal):
    fake_query = mock.MagicMock()
    fake_query.filter.return_value.first.return_value = goal
    fake_db = mock.MagicMock()
    fake_db.query.return_value = fake_query
    fake_db.close = mock.Mock()
    fake_db.commit = mock.Mock()
    fake_db.rollback = mock.Mock()

    fake_models = SimpleNamespace(
        AgentGoal=type('AgentGoal', (), {'id': 'id'}),
        get_db=lambda: fake_db,
        CodingGoal=type('CodingGoal', (), {}),
        User=type('User', (), {}),
    )
    monkeypatch.setitem(sys.modules,
                        'integrations.social.models', fake_models)
    # THROUGH monkeypatch, so it is UNDONE — see the same fix in
    # test_dashboard_snapshot.py for the full account. A raw
    # `pkg.models = fake_models` is permanent: setitem restores sys.modules,
    # but a plain attribute assignment on the real package object is never
    # reverted, and `from integrations.social import models` prefers the
    # package ATTRIBUTE. Every later test in the process then sees this
    # SimpleNamespace instead of the real models module.
    pkg = sys.modules.get('integrations.social')
    if pkg is not None:
        monkeypatch.setattr(pkg, 'models', fake_models, raising=False)
    return fake_db


def _make_goal(goal_id='agent-1', status='active', owner_id='user-42',
               prompt_id='p-99'):
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
        last_dispatched_at=None,
    )


# ─── get_a2a_graph ────────────────────────────────────────────────────

def test_a2a_empty_when_no_delegations(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    _install_fake_iac(monkeypatch, delegations={})

    out = ds.get_a2a_graph('agent-1', depth=2)
    assert out['nodes'] == []
    assert out['edges'] == []
    assert out['root_id'] == 'agent-1'


def test_a2a_returns_delegate_when_root_is_from_agent(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    delegations = {
        'd1': {'from_agent': 'agent-1', 'to_agent': 'agent-2',
               'status': 'in_progress', 'created_at': '2026-05-17T10:00:00'},
    }
    _install_fake_iac(monkeypatch, delegations=delegations)

    out = ds.get_a2a_graph('agent-1')
    node_ids = {n['id'] for n in out['nodes']}
    assert node_ids == {'agent-1', 'agent-2'}
    roles = {n['id']: n['role'] for n in out['nodes']}
    assert roles['agent-1'] == 'root'
    assert roles['agent-2'] == 'delegate'
    assert len(out['edges']) == 1
    assert out['edges'][0]['from'] == 'agent-1'
    assert out['edges'][0]['to'] == 'agent-2'
    assert out['edges'][0]['status'] == 'in_progress'


def test_a2a_returns_delegator_when_root_is_to_agent(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    delegations = {
        'd1': {'from_agent': 'agent-parent', 'to_agent': 'agent-1',
               'status': 'completed', 'created_at': '2026-05-17T09:00:00'},
    }
    _install_fake_iac(monkeypatch, delegations=delegations)

    out = ds.get_a2a_graph('agent-1')
    roles = {n['id']: n['role'] for n in out['nodes']}
    assert roles['agent-1'] == 'root'
    assert roles['agent-parent'] == 'delegator'


def test_a2a_ignores_unrelated_delegations(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    delegations = {
        'd1': {'from_agent': 'agent-1', 'to_agent': 'agent-2'},
        'd2': {'from_agent': 'agent-99', 'to_agent': 'agent-100'},  # unrelated
    }
    _install_fake_iac(monkeypatch, delegations=delegations)

    out = ds.get_a2a_graph('agent-1')
    node_ids = {n['id'] for n in out['nodes']}
    assert node_ids == {'agent-1', 'agent-2'}
    assert len(out['edges']) == 1


def test_a2a_handles_missing_iac_module_gracefully(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    # Don't install fake module — import raises ImportError.
    monkeypatch.setitem(sys.modules, 'integrations.internal_comm', None)

    out = ds.get_a2a_graph('agent-1')
    assert out['nodes'] == []
    assert out['edges'] == []


# ─── steer_agent ──────────────────────────────────────────────────────

def test_steer_pause_sets_status_paused(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='active')
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    captured = _install_fake_audit(monkeypatch)

    out = ds.steer_agent(fake_db, 'agent-1', 'pause', actor_id='alice')

    assert out['ok'] is True
    assert out['new_status'] == 'paused'
    assert goal.status == 'paused'
    fake_db.commit.assert_called_once()
    assert len(captured) == 1
    assert captured[0]['event_type'] == 'agent_steered'
    assert captured[0]['actor_id'] == 'alice'


def test_steer_resume_requires_paused(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='active')
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    _install_fake_audit(monkeypatch)

    out = ds.steer_agent(fake_db, 'agent-1', 'resume')

    assert out['ok'] is False
    assert 'paused' in out['error']
    assert goal.status == 'active'  # unchanged
    fake_db.commit.assert_not_called()


def test_steer_resume_from_paused_succeeds(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='paused')
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    _install_fake_audit(monkeypatch)

    out = ds.steer_agent(fake_db, 'agent-1', 'resume')

    assert out['ok'] is True
    assert out['new_status'] == 'active'
    assert goal.status == 'active'


def test_steer_cancel_archives(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='active')
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    _install_fake_audit(monkeypatch)

    out = ds.steer_agent(fake_db, 'agent-1', 'cancel')

    assert out['ok'] is True
    assert out['new_status'] == 'archived'


def test_steer_cancel_blocks_already_archived(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal(status='archived')
    fake_db = _install_fake_models(monkeypatch, goal=goal)
    _install_fake_audit(monkeypatch)

    out = ds.steer_agent(fake_db, 'agent-1', 'cancel')

    assert out['ok'] is False
    assert 'archived' in out['error']


def test_steer_unknown_verb_rejected(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    goal = _make_goal()
    fake_db = _install_fake_models(monkeypatch, goal=goal)

    out = ds.steer_agent(fake_db, 'agent-1', 'detonate')
    assert out['ok'] is False
    assert 'detonate' in out['error']


def test_steer_missing_agent_returns_error(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=None)

    out = ds.steer_agent(fake_db, 'nope', 'pause')
    assert out['ok'] is False
    assert 'not found' in out['error']


# ─── inject_instruction ───────────────────────────────────────────────

def test_inject_rejects_empty_instruction(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=_make_goal())

    out = ds.inject_instruction(fake_db, 'agent-1', '   ')
    assert out['ok'] is False
    assert 'empty' in out['error']


def test_inject_no_groupchat_registered(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=_make_goal())
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks',
                        SimpleNamespace(get_registered_groupchat=lambda key: None))

    out = ds.inject_instruction(fake_db, 'agent-1', 'retry now')
    assert out['ok'] is False
    assert 'no live GroupChat' in out['error']


def test_inject_appends_to_groupchat_and_audits(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=_make_goal(
        owner_id='user-42', prompt_id='p-99'))

    fake_gc = SimpleNamespace(messages=[
        {'role': 'assistant', 'content': 'first turn'},
    ])
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks',
                        SimpleNamespace(get_registered_groupchat=lambda key: fake_gc))
    captured = _install_fake_audit(monkeypatch)

    out = ds.inject_instruction(fake_db, 'agent-1', 'switch to cloud',
                                actor_id='alice')

    assert out['ok'] is True
    assert out['message_index'] == 1
    assert len(fake_gc.messages) == 2
    assert fake_gc.messages[1]['role'] == 'user'
    assert fake_gc.messages[1]['content'] == 'switch to cloud'
    assert 'alice' in fake_gc.messages[1]['name']
    assert len(captured) == 1
    assert captured[0]['action'] == 'inject'


def test_inject_missing_agent_returns_error(monkeypatch):
    ds = pytest.importorskip('integrations.social.dashboard_service')
    fake_db = _install_fake_models(monkeypatch, goal=None)

    out = ds.inject_instruction(fake_db, 'nope', 'hi')
    assert out['ok'] is False
    assert 'not found' in out['error']


# ─── _compute_eta_from_tree ───────────────────────────────────────────

def test_eta_none_for_empty_tree():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    assert ds._compute_eta_from_tree([]) is None


def test_eta_none_when_insufficient_samples_and_no_running():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    tree = [
        {'task_id': 't1', 'status': 'completed',
         'created_at': '2026-05-17T10:00:00',
         'updated_at': '2026-05-17T10:01:00'},
    ]
    # Only 1 completed sample, no in_progress — return None.
    assert ds._compute_eta_from_tree(tree) is None


def test_eta_computes_avg_p95_from_completed():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    base = datetime(2026, 5, 17, 10, 0, 0)
    tree = [
        {'task_id': f't{i}', 'status': 'completed',
         'created_at': base.isoformat(),
         'updated_at': (base + timedelta(seconds=10 * (i + 1))).isoformat()}
        for i in range(5)
    ]
    out = ds._compute_eta_from_tree(tree, now=base + timedelta(seconds=200))
    assert out is not None
    # durations: 10, 20, 30, 40, 50 → avg=30, p95≈50
    assert out['avg_seconds'] == 30
    assert out['p95_seconds'] == 50
    assert out['samples'] == 5


def test_eta_includes_elapsed_for_in_progress():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    base = datetime(2026, 5, 17, 10, 0, 0)
    tree = [
        {'task_id': 't1', 'status': 'completed',
         'created_at': base.isoformat(),
         'updated_at': (base + timedelta(seconds=60)).isoformat()},
        {'task_id': 't2', 'status': 'completed',
         'created_at': base.isoformat(),
         'updated_at': (base + timedelta(seconds=120)).isoformat()},
        {'task_id': 't3', 'status': 'in_progress',
         'created_at': (base + timedelta(seconds=150)).isoformat(),
         'updated_at': None},
    ]
    now = base + timedelta(seconds=300)
    out = ds._compute_eta_from_tree(tree, now=now)
    assert out is not None
    assert out['elapsed_seconds'] == 150  # 300 - 150
    assert out['avg_seconds'] == 90       # avg of 60, 120


def test_eta_just_elapsed_when_no_completed_samples():
    ds = pytest.importorskip('integrations.social.dashboard_service')
    base = datetime(2026, 5, 17, 10, 0, 0)
    tree = [
        {'task_id': 't1', 'status': 'in_progress',
         'created_at': base.isoformat(),
         'updated_at': None},
    ]
    now = base + timedelta(seconds=45)
    out = ds._compute_eta_from_tree(tree, now=now)
    assert out is not None
    assert out['elapsed_seconds'] == 45
    assert 'avg_seconds' not in out
    assert out['samples'] == 0
