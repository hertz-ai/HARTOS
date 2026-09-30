"""A distributed goal's requester never leaves the node; a handle travels instead.

Owner's egress ruling (2026-09-26): a real user id must not travel to other
people's nodes.  dispatch_goal_distributed put the requester's user id in the
submitted context, which goes to the coordinator ledger and to gossip peers.
It now carries an opaque per-goal handle
(integrations.distributed_agent.requesters), and:

* a remote worker runs and reports under the handle (it has no such user);
* the originating node maps the handle back: its own worker runs the task
  as the real user, and the goal-contribution notification reaches the real
  person;
* a handle another node minted resolves to nobody here, so no notification
  is written for it.

Review of e9daad6c5 (approved, follow-ups):
* a goal already in the ledger kept the real id: re-dispatch reused its
  tasks untouched, and /tasks/available served the whole context.  The
  re-dispatch now swaps the handle in, and /tasks/available and /tasks/claim
  serve contexts without a user id;
* resolve_requester returned any non-handle value unchanged, so a peer could
  name a local user outright and a pulled task ran /chat as them.  A raw id
  is trusted only when the task's source_node is this node, and a peer's
  announce cannot claim that: ingest drops its user_id and source_node;
* the handle tables live in the coordinator's own storage directory, so they
  are shared exactly when the coordinator's store is.

Behavioural: the real dispatch, coordinator (in-memory ledger + lock), worker
loop, blueprint routes, GossipTaskBridge and NotificationService on in-memory
SQLite.  Patched boundaries: the coordinator lookup, this node's identity,
the peer list and the HTTP post, the /chat call, guardrails, the realtime
push, the token store, and the data directory (to tmp_path).
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

REAL_USER = 'd68c9dee-real-person'
HERE = 'node-here-6f1c'


@pytest.fixture(autouse=True)
def _tables(tmp_path, monkeypatch):
    """The data dir is tmp_path; this node is HERE."""
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from core.file_cache import invalidate_file_cache
    invalidate_file_cache()
    with patch('core.platform_paths.get_agent_data_dir',
               return_value=str(tmp_path)), \
         patch('integrations.distributed_agent.requesters.this_node_id',
               return_value=HERE):
        yield os.path.join(str(tmp_path), 'distributed_tasks')
    invalidate_file_cache()


class _MemBackend:
    def __init__(self):
        self.data = {}
        self.saves = 0

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.saves += 1
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


def _ledger_text(led):
    return repr([vars(led.get_task(t)) for t in led.task_order])


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


@pytest.fixture
def client():
    """The distributed blueprint with a signed-in peer node's token."""
    from flask import Flask
    from integrations.distributed_agent.api import distributed_agent_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(distributed_agent_bp)
    peer = SimpleNamespace(id='peer-node-user', is_admin=False, role='flat',
                           is_banned=False)
    with patch('integrations.social.auth._get_user_from_token',
               return_value=(peer, MagicMock())), \
         patch('integrations.distributed_agent.api._ensure_delegation_subscriber',
               return_value=False):
        yield app.test_client()


AUTH = {'Authorization': 'Bearer t'}


# ── the peer-bound payload ──────────────────────────────────────────────

def test_the_submitted_context_carries_a_handle_not_the_user():
    led, coord = _coordinator()
    gid = _dispatch(coord)
    assert gid
    assert REAL_USER not in _ledger_text(led)
    parent = led.get_task(gid)
    handle = parent.context['user_id']
    assert handle.startswith('req_')
    assert parent.context['source_node'] == HERE
    from integrations.distributed_agent.requesters import resolve_requester
    assert resolve_requester(handle, HERE) == REAL_USER


def test_the_same_goal_keeps_its_handle():
    from integrations.distributed_agent.requesters import requester_handle
    a = requester_handle('g-x', REAL_USER)
    assert requester_handle('g-x', REAL_USER) == a
    assert requester_handle('g-y', REAL_USER) != a


def test_a_goal_already_in_the_ledger_gets_the_handle_on_re_dispatch():
    """Review of e9daad6c5: re-dispatch reused a legacy task set untouched,
    so REAL-USER-OLD stayed in the ledger and was served to peers."""
    led, coord = _coordinator()
    coord.submit_goal('Recruit compute',
                      [{'task_id': 'gL_task_0', 'description': 'd'}],
                      {'user_id': 'REAL-USER-OLD', 'source_node': 'unknown'},
                      goal_id='gL')
    assert 'REAL-USER-OLD' in _ledger_text(led)
    _dispatch(coord, goal_id='gL', user='REAL-USER-OLD')
    assert 'REAL-USER-OLD' not in _ledger_text(led)
    from integrations.distributed_agent.requesters import resolve_requester
    for tid in ('gL', 'gL_task_0'):
        ctx = led.get_task(tid).context
        assert ctx['user_id'].startswith('req_'), tid
        assert ctx['source_node'] == HERE, tid
        assert resolve_requester(ctx['user_id'], ctx['source_node']) == 'REAL-USER-OLD'


def test_a_re_dispatch_that_changes_nothing_writes_nothing():
    """The dedup branch runs every tick for every in-flight goal: an
    unconditional save there is the json.dump storm behind #145."""
    led, coord = _coordinator()
    _dispatch(coord)
    before = led.backend.saves
    _dispatch(coord)
    assert led.backend.saves == before


def test_tasks_available_and_claim_serve_no_user(client):
    led, coord = _coordinator()
    coord.submit_goal('o', [{'task_id': 'gA_task_0', 'description': 'd'}],
                      {'user_id': 'REAL-USER-OLD', 'goal_type': 'x'},
                      goal_id='gA')
    _dispatch(coord, goal_id='gB')
    with patch('integrations.distributed_agent.api._get_coordinator',
               return_value=coord):
        available = client.get('/api/distributed/tasks/available', headers=AUTH)
        claimed = client.post('/api/distributed/tasks/claim',
                              json={'agent_id': 'peer-node-user'}, headers=AUTH)
    assert available.status_code == 200
    body = available.get_data(as_text=True)
    assert 'REAL-USER-OLD' not in body and REAL_USER not in body
    tasks = available.get_json()['tasks']
    assert tasks and all('user_id' not in t['context'] for t in tasks)
    assert tasks[0]['context'].get('goal_type') == 'x'   # the rest is served
    assert claimed.status_code == 200
    assert 'user_id' not in claimed.get_json()['context']
    assert REAL_USER not in claimed.get_data(as_text=True)
    # ...and the ledger itself still knows who asked.
    assert led.get_task('gB_task_0').context['user_id'].startswith('req_')


def test_the_gossip_announce_carries_no_user(client):
    """POST /api/distributed/goals announces the goal to every peer
    (GossipTaskBridge.announce_goal, plaintext to a peer without X25519):
    that payload names nobody, even when the body tried to."""
    _, coord = _coordinator()
    sent = []

    def _post(url, json=None, timeout=None):
        sent.append((url, json))
        return SimpleNamespace(status_code=200)

    peers = [{'host_url': 'http://peer.example', 'node_id': 'p1',
              'x25519_public': ''}]
    with patch('integrations.distributed_agent.api._get_coordinator',
               return_value=coord), \
         patch('integrations.distributed_agent.coordinator_backends.'
               'GossipTaskBridge._get_active_peers', return_value=peers), \
         patch('core.http_pool.pooled_post', side_effect=_post):
        r = client.post('/api/distributed/goals', json={
            'objective': 'o', 'tasks': [{'task_id': 'gN_t0', 'description': 'd'}],
            'context': {'user_id': REAL_USER, 'repo_url': 'a/b'}}, headers=AUTH)
    assert r.status_code == 200, r.get_json()
    announces = [p for u, p in sent if u.endswith('/api/distributed/tasks/announce')]
    assert announces, 'the goal was never announced'
    for payload in announces:
        assert REAL_USER not in json.dumps(payload)
        assert 'user_id' not in payload['context']
        assert payload['context'].get('repo_url') == 'a/b'


# ── a raw id is trusted only from this node ─────────────────────────────

def test_a_raw_id_resolves_only_when_this_node_stamped_it():
    from integrations.distributed_agent.requesters import resolve_requester
    assert resolve_requester(REAL_USER, HERE) == REAL_USER
    for source in ('peer-node-9', '', None, 'unknown', 'none'):
        assert resolve_requester(REAL_USER, source) is None, source


def test_a_peer_cannot_name_a_local_user_through_an_announce(client, Session):
    """A peer announces a task naming a local user and claiming to be this
    node.  Our worker must not run /chat as that user, and nobody here is
    notified on its behalf."""
    led, coord = _coordinator()
    with patch('integrations.distributed_agent.api._get_coordinator',
               return_value=coord):
        r = client.post('/api/distributed/tasks/announce', json={
            'goal_id': 'peer-g', 'objective': 'o',
            'tasks': [{'task_id': 'peer-g_t0', 'description': 'd'}],
            'context': {'user_id': REAL_USER, 'source_node': HERE}},
            headers=AUTH)
    assert r.status_code == 200, r.get_json()
    assert REAL_USER not in _ledger_text(led)
    assert _run_worker(coord) != REAL_USER
    assert _notify(Session, coord, 'peer-g_t0') == []


def test_a_forged_source_node_already_in_a_ledger_is_still_refused(Session):
    """Defence in depth for a context that reached the ledger before ingest
    stripped it: a raw id is trusted only with THIS node's source_node --
    and a context whose source_node is this node but whose value is not a
    handle this node minted is the one case the rule admits, which is why
    ingest must strip it.  A foreign source_node is refused."""
    led, coord = _coordinator()
    coord.submit_goal('o', [{'task_id': 'gF_t0', 'description': 'd'}],
                      {'user_id': REAL_USER, 'source_node': 'peer-node-9'},
                      goal_id='gF')
    assert _run_worker(coord) != REAL_USER
    assert _notify(Session, coord, 'gF_t0') == []


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
    for f in os.listdir(_tables):
        if f.startswith('distributed_'):
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
                      {'user_id': 'req_' + 'ab' * 12, 'source_node': HERE},
                      goal_id='g9')
    assert _notify(Session, coord, 'g9_task_0') == []


# ── where the tables live (review of e9daad6c5, item 3) ─────────────────

def test_the_handle_tables_live_beside_the_coordinator_ledger(_tables):
    """The coordinator's store is a JSON ledger in its storage directory
    (the Redis backend never builds: coordinator_backends._try_redis_backend
    names an undefined `host`).  The handle tables are written into that same
    directory, so they are shared exactly when the coordinator's store is."""
    from integrations.distributed_agent.coordinator_backends import (
        _create_inmemory_backend, coordinator_storage_dir)
    coord = _create_inmemory_backend('local')
    assert coord is not None
    _dispatch(coord, goal_id='gS')
    store = coordinator_storage_dir()
    files = set(os.listdir(store))
    assert 'distributed_handles.json' in files
    assert any(f.startswith('ledger_') for f in files), files


def test_re_recording_a_submitter_makes_it_the_newest(monkeypatch):
    """Eviction is strictly oldest-first: a re-recorded goal moves to the
    end, so it outlives goals recorded after its first record."""
    from integrations.distributed_agent import requesters
    monkeypatch.setattr(requesters, '_MAX_ROWS', 3)
    for gid in ('a', 'b', 'c'):
        requesters.record_submitter(gid, 'u-' + gid)
    requesters.record_submitter('a', 'u-a')
    requesters.record_submitter('d', 'u-d')
    assert requesters.submitter_of('b') is None
    assert requesters.submitter_of('a') == 'u-a'
    assert requesters.submitter_of('d') == 'u-d'
