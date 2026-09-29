"""A notification is pushed once, by NotificationService.create, after its
row commits: never a second time by the caller, never before the commit.

create() queues the push with models.after_commit (79928c2fc).  Three callers
still pushed it again themselves (measured with this file):
  - agent_daemon._send_hitl_notification: twice, and the extra push went
    out BEFORE the daemon committed, for a row that could still roll back;
  - verification_protocol._notify_verification_accepted: twice;
  - deploy/claude_notify.notify: twice (its comment called it harmless;
    the payload carries no msg_id, so a client has nothing to dedupe on).
74d32bc69 fixed the same thing in task_coordinator.  The guard at the bottom
fails CI on a fourth.

Real SQLite; only the realtime push is recorded.
"""
import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


@pytest.fixture
def factory(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social import models as social_models
    from integrations.social.models import Base, User
    eng = create_engine(f"sqlite:///{tmp_path / 'push.db'}")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(social_models, '_SessionLocal', f)
    s = f()
    s.add(User(id='owner-1', username='u_owner-1', display_name='o',
               user_type='human'))
    s.commit()
    s.close()
    yield f
    eng.dispose()


@pytest.fixture
def pushes():
    sent = []
    with patch('integrations.social.realtime.on_notification',
               side_effect=lambda uid, d: sent.append((uid, d.get('id')))):
        yield sent


def _ids(factory, type_):
    from integrations.social.models import Notification
    s = factory()
    try:
        return [n.id for n in s.query(Notification).filter_by(type=type_)]
    finally:
        s.close()


def test_hitl_notice_is_pushed_once_and_only_after_the_commit(factory, pushes):
    import integrations.agent_engine.agent_daemon as daemon
    daemon._hitl_notified.discard('g1:t1')
    goal = SimpleNamespace(id='g1', created_by='owner-1', owner_id=None)
    task = SimpleNamespace(id='t1', description='review this step')
    db = factory()
    try:
        daemon._send_hitl_notification(db, goal, task)
        assert pushes == [], 'pushed before the row committed'
        db.commit()
    finally:
        db.close()
    rows = _ids(factory, 'approval_required')
    assert len(rows) == 1
    assert pushes == [('owner-1', rows[0])]


def test_verification_notice_is_pushed_once(factory, pushes):
    from integrations.distributed_agent.verification_protocol import (
        VerificationProtocol)
    proto = VerificationProtocol.__new__(VerificationProtocol)
    task = SimpleNamespace(context={'claimed_by': 'owner-1'},
                           parent_task_id=None)
    proto._ledger = SimpleNamespace(get_task=lambda _tid: task)
    proto._notify_verification_accepted('task-9')
    rows = _ids(factory, 'goal_verified')
    assert len(rows) == 1
    assert pushes == [('owner-1', rows[0])]


def test_the_claude_notify_script_pushes_once(factory, pushes, monkeypatch):
    sys.path.insert(0, str(_ROOT / 'deploy'))
    try:
        import claude_notify
    finally:
        sys.path.remove(str(_ROOT / 'deploy'))
    monkeypatch.setattr(claude_notify, '_resolve_user_id', lambda: 'owner-1')
    nid = claude_notify.notify('hello from the orchestrator')
    assert pushes == [('owner-1', nid)]


# ── guard: the push belongs to NotificationService.create ──────────────

def _double_pushers(tree):
    """Functions that call NotificationService.create AND on_notification."""
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        creates = pushes = False
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if (name == 'create' and isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == 'NotificationService'):
                creates = True
            if name == 'on_notification':
                pushes = True
        if creates and pushes:
            found.append((fn.lineno, fn.name))
    return found


def test_source_guard_no_caller_pushes_what_create_already_pushes():
    offenders = []
    for top in ('hartos', 'integrations', 'core', 'security', 'deploy'):
        for path in (_ROOT / top).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            rel = path.relative_to(_ROOT).as_posix()
            if rel == 'integrations/social/services.py':
                continue   # create() itself
            try:
                tree = ast.parse(path.read_text(encoding='utf-8',
                                                errors='replace'))
            except SyntaxError:
                continue
            offenders += [f'{rel}:{ln} {fn}' for ln, fn in _double_pushers(tree)]
    assert offenders == [], (
        'NotificationService.create already pushes after the commit; a '
        'second on_notification pushes the card twice: ' + ', '.join(offenders))


def test_source_guard_sees_a_double_push():
    tree = ast.parse(
        'def f(db):\n'
        '    n = NotificationService.create(db, "u", "t")\n'
        '    from integrations.social.realtime import on_notification\n'
        '    on_notification("u", n.to_dict())\n'
        'def g(db):\n'
        '    NotificationService.create(db, "u", "t")\n')
    assert [fn for _, fn in _double_pushers(tree)] == ['f']
