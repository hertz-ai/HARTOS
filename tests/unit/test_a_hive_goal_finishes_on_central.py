"""A goal central hands to the hive finishes: only a live peer makes a hive,
central's own worker runs the hive turn on the model a local turn would use,
and work done here closes the hive task it belongs to.

Measured on central 2026-11-18 (read-only; D:/claude_scratch/99c39a1e/
rca_ledger_grounding_1119.md):
- 4 'active' peer_nodes rows (central itself and three private 192.168.x
  peers) sent every earning goal to the hive.  63 of the 63 claimed tasks
  were claimed by central's own node id: nobody else ever took one.
- That worker ran its turns on the node's own (free) model; a local turn of
  the same goal names the configured expert (dispatch.revenue_expert_config).
  57 free-model calls in 05:34-05:43Z, and every claim in them ended "held
  for help".
- The expert's step then ran here, on the daemon, and nothing closed the
  hive task held for it.  The local settle still read the hive ledger, so
  goals 2669bc6c and 561bcef9 paused "work was dispatched and spark was
  spent, but the ledger never reported every task done" 3086 s and 3000 s
  after their first handoff.

Behavioural: the real AgentDaemon._tick, dispatch_goal, _has_hive_peers
(over a real peer_nodes table in SQLite), DistributedTaskCoordinator
(in-memory ledger and lock), DistributedWorkerLoop._tick and
_settle_dispatched_goal.  Replaced at the boundaries only: the goal row, the
idle personas, the yield / budget / guardrail gates, the prompt builder, the
configured expert (the model registry), the gossip node's identity, the /chat
turn (local_chat_dispatch, which charges spark the way
charge_goal_work_completed does when a flow completes), the recipe file the
expert's step banks, the audit log and the world-model bridge.

    python -m pytest tests/unit/test_a_hive_goal_finishes_on_central.py -q
"""
import contextlib
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from integrations.agent_engine import agent_daemon, dispatch  # noqa: E402
from integrations.agent_engine.agent_daemon import AgentDaemon  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from integrations.distributed_agent.task_coordinator import (  # noqa: E402
    DistributedTaskCoordinator)
from integrations.distributed_agent.worker_loop import (  # noqa: E402
    DistributedWorkerLoop)
from tests.unit.module_swap import swap_modules  # noqa: E402

GOAL = 'goal-central-1'
HERE = 'node-central-0a1b'
TASK = f'{GOAL}_task_0'
#: The configured expert's one entry, as ModelBackend.to_config_list gives it.
EXPERT = [{'model': 'claude-haiku-4-5', 'api_key': 'test-key',
           'base_url': 'https://api.anthropic.com/v1'}]
STALE_S = 300


class Goal:
    """Stands in for an AgentGoal row (the attributes the tick, dispatch and
    the worker read).  Seeded: it names no owner, so it is the node's own."""

    def __init__(self, cfg=None, spark=0):
        self.id = GOAL
        self.goal_type = 'marketing'
        self.title = 'Find buyers'
        self.description = ''
        self.status = 'active'
        self.config_json = dict(cfg or {})
        self.spark_spent = spark
        self.spark_budget = 500
        self.last_dispatched_at = None
        self.product_id = None
        self.owner_id = None
        self.created_by = None
        self.user_id = None
        self.prompt_id = None
        self.flow_id = None

    def to_dict(self):
        d = {'id': self.id, 'goal_type': self.goal_type, 'title': self.title,
             'description': self.description, 'status': self.status}
        d.update(self.config_json)
        return d


class _GoalQuery:
    """The goal-row reads: the row itself, or (status, config_json) for the
    worker's column query."""

    def __init__(self, goal, columns):
        self._goal, self._columns = goal, columns

    def filter(self, *a, **k):
        return self

    def filter_by(self, **k):
        return self

    def first(self):
        if self._columns:
            return (self._goal.status, self._goal.config_json)
        return self._goal


class _Session:
    """What db_session() yields: the one goal for goal reads, and the real
    peer_nodes table for _has_hive_peers."""

    def __init__(self, goal, peers):
        self._goal, self._peers = goal, peers

    def query(self, *entities):
        from integrations.social.models import PeerNode
        if entities and entities[0] is PeerNode:
            return self._peers.query(*entities)
        return _GoalQuery(self._goal, columns=len(entities) > 1)


def peer_table(rows):
    """A real SQLite peer_nodes table holding ``rows``."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import PeerNode
    engine = create_engine('sqlite://')
    PeerNode.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    for row in rows:
        session.add(PeerNode(**row))
    session.commit()
    return session


def _row(node_id, url, age_s):
    return {'node_id': node_id, 'url': url, 'status': 'active',
            'last_seen': datetime.utcnow() - timedelta(seconds=age_s)}


def central_peers():
    """Central's table on 2026-11-18: its own row and three LAN peers, all
    'active'; the three last heard from two hours ago."""
    return peer_table([_row(HERE, 'https://central.example', 5)] + [
        _row(f'lan-peer-{i}', f'http://192.168.0.1{i}:5000', 7200)
        for i in range(3)])


def alone():
    """Only this node's own row: no other node, so goals run here."""
    return peer_table([_row(HERE, 'https://central.example', 5)])


def with_live_peer():
    """The same table, plus one peer heard from 30 s ago."""
    return peer_table([_row(HERE, 'https://central.example', 5),
                       _row('lan-peer-0', 'http://192.168.0.10:5000', 7200),
                       _row('live-peer', 'https://peer.example', 30)])


@contextlib.contextmanager
def coordinator(tmp):
    """An in-memory coordinator; the requester tables in ``tmp``."""
    from core.file_cache import invalidate_file_cache
    invalidate_file_cache()
    led = SmartLedger('coord', 'hive', str(tmp), backend=InMemoryBackend())
    co = DistributedTaskCoordinator(ledger=led, task_lock=InMemoryTaskLock(),
                                    verifier=MagicMock(), baseline=MagicMock())
    with patch.dict(os.environ, {'HEVOLVE_DB_PATH': ':memory:'}), \
         patch('core.platform_paths.get_agent_data_dir', return_value=str(tmp)), \
         patch('integrations.distributed_agent.requesters.this_node_id',
               return_value=HERE):
        try:
            yield co
        finally:
            invalidate_file_cache()


@pytest.fixture
def coord(tmp_path):
    with coordinator(tmp_path) as co:
        yield co


def _expert_backend():
    backend = MagicMock()
    backend.to_config_list.return_value = [dict(EXPERT[0])]
    backend.is_dispatchable.return_value = True
    return backend


def _boundaries(goal, coord, chat, peers):
    """The patches every tick here runs under."""
    from integrations.social.peer_discovery import get_peer_discovery

    @contextlib.contextmanager
    def _db_session(*a, **k):
        yield _Session(goal, peers)

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [goal]
    from integrations.agent_engine.model_registry import model_registry
    return [
        patch('integrations.social.models.get_db', return_value=db),
        patch('integrations.social.models.db_session', _db_session),
        patch.object(get_peer_discovery(), 'node_id', HERE),
        patch.object(get_peer_discovery(), 'stale_threshold', STALE_S),
        patch('integrations.coding_agent.idle_detection.IdleDetectionService.'
              'get_idle_agent_personas',
              return_value=[{'user_id': 'agent-1', 'username': 'a1'}]),
        patch.object(dispatch, 'should_yield_to_user', return_value=False),
        patch.object(dispatch, 'local_dispatch_provider_breaker_open',
                     return_value=''),
        patch.object(dispatch, 'local_dispatch_llm_busy', return_value=False),
        patch.object(dispatch, '_dispatch_provider_host', return_value=''),
        # The configured expert, offered (the registry's lane and cap gate).
        patch.object(model_registry, 'get_configured_expert',
                     side_effect=lambda *a, **k: _expert_backend()),
        patch.object(model_registry, 'get_model',
                     side_effect=lambda *a, **k: _expert_backend()),
        patch('core.autogen_config.resolve_expert_backend', return_value=None),
        patch.object(agent_daemon, '_flow_recipe_exists', return_value=True),
        patch('integrations.agent_engine.goal_manager.GoalManager.build_prompt',
              return_value='Find buyers for the cafe app'),
        patch('integrations.agent_engine.budget_gate.estimate_llm_cost_spark',
              return_value=0),
        patch('integrations.agent_engine.budget_gate.pre_dispatch_budget_gate',
              return_value=(True, 'OK')),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda prompt, *a, **k: (True, '', prompt)),
        patch('security.hive_guardrails.GuardrailEnforcer.after_response',
              return_value=(True, '')),
        patch('security.immutable_audit_log.get_audit_log',
              return_value=MagicMock()),
        patch('integrations.agent_engine.world_model_bridge.'
              'get_world_model_bridge', return_value=MagicMock()),
        patch.object(AgentDaemon, '_try_parallel_dispatch',
                     return_value={'completed': 0, 'failed': 0, 'deferred': 0}),
        patch.object(dispatch, '_decompose_goal',
                     side_effect=lambda prompt, gid, gt, uid: [{
                         'task_id': f'{gid}_task_0', 'description': prompt,
                         'capabilities': []}]),
        patch.object(dispatch, '_get_distributed_coordinator',
                     return_value=coord),
        patch.object(dispatch, 'local_chat_dispatch', chat),
        swap_modules({'routes.hartos_backend_adapter': None}),
    ]


def _run(patches, fn):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return fn()


def daemon_tick(goal, coord, chat, peers, extra=()):
    """One real agent-daemon tick over ``goal``."""
    agent_daemon._dispatch_backoff.pop(str(goal.id), None)
    agent_daemon._budget_blocked_goals.discard(str(goal.id))
    goal.last_dispatched_at = None
    _run(list(_boundaries(goal, coord, chat, peers)) + list(extra),
         lambda: AgentDaemon()._tick())


def worker_tick(goal, coord, chat, peers):
    """One real distributed-worker tick on this node."""
    loop = DistributedWorkerLoop()
    loop._node_id = HERE
    with patch.object(loop, '_get_coordinator', return_value=coord):
        _run(_boundaries(goal, coord, chat, peers), loop._tick)


def charging_chat(goal, spark=2, reply='Drafted the email to three cafes.'):
    """The /chat turn: its flow completes and charges the goal's spark."""
    def _turn(*a, **k):
        goal.spark_spent = (goal.spark_spent or 0) + spark
        return ('ok', reply)
    return MagicMock(side_effect=_turn)


def held_by_an_earlier_handoff(coord):
    """The goal's hive task as central's worker left it: claimed, its turn
    handed to the expert, held (BLOCKED, input_required)."""
    coord.submit_goal('Find buyers', [{'task_id': TASK, 'description':
                                       'Find buyers', 'capabilities': []}],
                      {'prompt': 'Find buyers'}, goal_id=GOAL)
    task = coord.claim_next_task(HERE, [])
    assert task.task_id == TASK
    assert coord.hold_task(TASK, HERE, 'Paused for help: the expert takes it')
    assert coord._ledger.get_task(TASK).status == TaskStatus.BLOCKED


def _task(coord):
    return coord._ledger.get_task(TASK)


# ── (b) a hive is a live peer, not an 'active' row ───────────────────────

def test_stale_active_rows_are_not_a_hive(coord):
    """FAILS BEFORE THE FIX: central's four 'active' rows (itself and three
    peers last heard from two hours ago) counted as a hive."""
    goal = Goal()
    with contextlib.ExitStack() as stack:
        for p in _boundaries(goal, coord, MagicMock(), central_peers()):
            stack.enter_context(p)
        assert dispatch._has_hive_peers() is False


def test_with_only_stale_peers_the_goal_runs_here_on_the_expert(coord):
    """FAILS BEFORE THE FIX: the goal went to the hive, where only central's
    own worker would ever claim it."""
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat, central_peers())

    assert chat.call_count == 1, 'the goal was handed to a hive of stale rows'
    assert chat.call_args.kwargs.get('model_config') == EXPERT
    assert goal.status == 'completed', goal.config_json
    assert goal.config_json['completion_grounding'] == 'local_dispatch_spend'


def test_a_peer_heard_from_within_the_stale_threshold_makes_a_hive(coord):
    goal = Goal()
    chat = charging_chat(goal)
    peers = with_live_peer()
    with contextlib.ExitStack() as stack:
        for p in _boundaries(goal, coord, chat, peers):
            stack.enter_context(p)
        assert dispatch._has_hive_peers() is True

    daemon_tick(goal, coord, chat, with_live_peer())
    assert chat.call_count == 0, 'a handoff must not also run the turn here'
    assert _task(coord).status == TaskStatus.PENDING
    assert goal.status == 'active'


# ── (c) central's worker runs the hive turn on the expert ────────────────

def test_the_worker_runs_an_earning_goals_hive_turn_on_the_expert(coord):
    """FAILS BEFORE THE FIX: the worker ran the turn on the node's own model
    (model_config None) while a local turn of the same goal names the
    configured expert."""
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat, with_live_peer())       # handed to the hive
    assert chat.call_count == 0

    worker_tick(goal, coord, chat, with_live_peer())       # central's worker
    assert chat.call_count == 1
    assert chat.call_args.kwargs.get('model_config') == EXPERT
    assert _task(coord).status == TaskStatus.COMPLETED

    daemon_tick(goal, coord, chat, with_live_peer())       # settled
    assert goal.status == 'completed', goal.config_json
    assert goal.config_json['completion_grounding'] == 'ledger_tasks_complete'


# ── (d) work done here closes the hive task it belongs to ────────────────

def test_a_local_turn_closes_the_task_an_earlier_handoff_left_held(coord):
    """FAILS BEFORE THE FIX: the turn ran here and spent spark, but the
    settle read the leftover held hive task and kept the goal waiting until
    the 1800 s clock paused it."""
    held_by_an_earlier_handoff(coord)
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat, alone())

    assert chat.call_count == 1
    assert goal.status == 'completed', goal.config_json
    assert 'awaiting_verification_since' not in goal.config_json
    task = _task(coord)
    assert task.status == TaskStatus.COMPLETED
    assert task.result == 'Drafted the email to three cafes.'
    assert task.context.get('claimed_by') == HERE


def test_the_experts_step_here_closes_the_hive_task_held_for_it(coord):
    """FAILS BEFORE THE FIX: the 2669bc6c shape.  The worker's turn handed
    its step to the expert and held the task; the daemon ran the expert's
    step here (a turn on a named model never goes to the hive), banked it,
    spent spark, and the goal still waited on the held task."""
    held_by_an_earlier_handoff(coord)
    pid = dispatch.prompt_id_for_goal(GOAL)
    goal = Goal(cfg={'escalation': {
        'next': 'expert', 'expert': EXPERT[0]['model'],
        'at': '2026-11-18T06:42:57', 'action_id': 1,
        'action': 'Write the email', 'user_prompt': 'Find buyers',
        'prompt_id': pid, 'flow': 0}})
    chat = charging_chat(goal)
    banked = tempfile.NamedTemporaryFile(delete=False)
    banked.close()
    helper = types.ModuleType('hartos.helper')
    helper.safe_prompt_path = lambda *a, **k: banked.name   # the step's recipe
    try:
        daemon_tick(goal, coord, chat, with_live_peer(),
                    extra=[swap_modules({'hartos.helper': helper})])
    finally:
        os.unlink(banked.name)

    assert chat.call_count == 1
    assert chat.call_args.kwargs.get('model_config') == EXPERT
    assert 'escalation' not in goal.config_json
    assert goal.status == 'completed', goal.config_json
    assert _task(coord).status == TaskStatus.COMPLETED


class _Db:
    def refresh(self, *a, **k):
        pass

    def flush(self):
        pass


def test_a_hive_handoff_whose_task_is_still_held_keeps_waiting(coord):
    """Unchanged: a goal handed to the hive is judged by its hive run, and a
    task still held is work outstanding, not closed."""
    held_by_an_earlier_handoff(coord)
    goal = Goal(cfg={'spark_at_dispatch': 0}, spark=4)
    with patch.object(dispatch, '_get_distributed_coordinator',
                      return_value=coord):
        agent_daemon._settle_dispatched_goal(_Db(), goal, GOAL,
                                             handed_to_hive=True)

    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert 'awaiting_verification_since' in goal.config_json
    assert _task(coord).status == TaskStatus.BLOCKED
