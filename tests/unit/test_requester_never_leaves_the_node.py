"""A distributed goal's requester never leaves the node; a handle travels instead.

Owner's egress ruling (2026-09-26): a real user id must not travel to other
people's nodes.  dispatch_goal_distributed put the requester's user id in the
submitted context, which goes to the shared coordinator ledger (hive Redis)
and to gossip peers.  It now carries an opaque per-goal handle
(integrations.distributed_agent.requesters), and:

* a remote worker runs and reports under the handle (it has no such user);
* the originating node maps the handle back: its own worker runs the task
  as the real user, and the goal-contribution notification reaches the real
  person;
* a handle another node minted resolves to nobody here, so no notification
  is written for it.

Behavioural: the real dispatch, coordinator (in-memory ledger + lock), worker
loop and NotificationService on in-memory SQLite.  Patched boundaries: the
coordinator lookup, the Redis peer probe, the /chat call, guardrails, the
world-model bridge, the realtime push, and the handle tables' directory.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

REAL_USER = 'd68c9dee-real-person'


@pytest.fixture(autouse=True)
def _tables(tmp_path):
    with patch('integrations.distributed_agent.requesters._dir',
               return_value=str(tmp_path)):
        yield tmp_path


class _MemBackend:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _coordinator():
    from agent_ledger.core import SmartLedger
    from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    led = SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())
    return led, DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())


def _dispatch(coord, goal_id='g-handle-1', user=REAL_USER):
    from integrations.agent_engine import dispatch as d
    with patch.object(d, '_get_distributed_coordinator', return_value=coord):
        return d.dispatch_goal_distributed('Recruit compute', user, goal_id,
                                           'hive_growth')


@pytest.fixture
def Session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import Base, User
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    maker = sessionmaker(bind=eng)
    s = maker()
    s.add(User(id=REAL_USER, username='real_person', display_name='R',
               user_type='human'))
    s.commit()
    s.close()
    return maker


# ── the peer-bound payload ──────────────────────────────────────────────

def test_the_submitted_context_carries_a_handle_not_the_user():
    led, coord = _coordinator()
    gid = _dispatch(coord)
    assert gid
    # Everything the shared ledger holds for the goal (it is what peers
    # read): the parent and every child task.
    stored = repr([led.get_task(t).to_dict() if hasattr(led.get_task(t), 'to_dict')
                   else vars(led.get_task(t)) for t in led.task_order])
    assert REAL_USER not in stored
    parent = led.get_task(gid)
    handle = parent.context['user_id']
    assert handle.startswith('req_')
    from integrations.distributed_agent.requesters import resolve_requester
    assert resolve_requester(handle) == REAL_USER


def test_the_same_goal_keeps_its_handle():
    from integrations.distributed_agent.requesters import requester_handle
    a = requester_handle('g-x', REAL_USER)
    assert requester_handle('g-x', REAL_USER) == a
    assert requester_handle('g-y', REAL_USER) != a


def test_a_gossip_announce_of_the_goal_carries_no_user(monkeypatch):
    """The coordinator's own announce path, if present, sends what the
    context holds: the handle."""
    led, coord = _coordinator()
    gid = _dispatch(coord)
    ctx = led.get_task(gid).context
    assert REAL_USER not in repr(ctx)


# ── the originating node maps it back ───────────────────────────────────

def _run_worker(coord):
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    loop = DistributedWorkerLoop()
    task = coord.claim_next_task('worker_a', capabilities=loop._capabilities)
    assert task is not None
    sent = {}

    def _chat(prompt, user_id, prompt_id, **kw):
        sent['user_id'] = user_id
        return 'deferred', None

    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               side_effect=lambda p: (True, '', p)), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch',
               side_effect=_chat):
        loop._execute_task(task)
    return sent['user_id']


def test_the_originating_nodes_worker_runs_as_the_real_user():
    _, coord = _coordinator()
    _dispatch(coord)
    assert _run_worker(coord) == REAL_USER


def test_a_remote_worker_runs_under_the_handle(_tables):
    _, coord = _coordinator()
    _dispatch(coord)
    # Another node: it never minted this handle (its tables are empty).
    import shutil
    for f in os.listdir(_tables):
        os.remove(os.path.join(_tables, f))
    from core.file_cache import invalidate_file_cache
    invalidate_file_cache()
    ran_as = _run_worker(coord)
    assert ran_as.startswith('req_')
    assert REAL_USER not in ran_as


def _notify(Session, coord, task_id):
    with patch('integrations.social.models.get_db', side_effect=Session), \
         patch('integrations.social.realtime.on_notification'):
        coord._notify_goal_contribution(task_id, agent_id='node-abc',
                                        task_description='d')
    from integrations.social.models import Notification
    s = Session()
    rows = [(n.user_id, n.type) for n in s.query(Notification).all()]
    s.close()
    return rows


def test_the_contribution_notification_reaches_the_real_person(Session):
    led, coord = _coordinator()
    gid = _dispatch(coord)
    child = [t for t in led.task_order if t != gid][0]
    rows = _notify(Session, coord, child)
    assert rows == [(REAL_USER, 'goal_contribution')]


def test_another_nodes_handle_notifies_nobody(Session):
    led, coord = _coordinator()
    coord.submit_goal('o', [{'task_id': 'g9_task_0', 'description': 'd'}],
                      {'user_id': 'req_' + 'ab' * 12}, goal_id='g9')
    assert _notify(Session, coord, 'g9_task_0') == []
