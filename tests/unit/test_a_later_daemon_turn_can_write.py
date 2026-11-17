"""Every daemon turn in a tick can write to the database, not only the first.

Measured on the owner's desktop 2026-11-07 (frozen_debug.log, read-only):
from 09:31 to 09:56 IST, agent 18895904180 (goal e0f826dc) asked its person
17 times. Every ask was logged "NOT filed: its row could not be written"
(sqlite3.OperationalError: database is locked). Between 08:40 and 09:56, 54
audit writes failed after 3 attempts, all on the agent_daemon thread. Those
included the 'goal_dispatched' entry of 8 different goals. The main database
file was written at 09:56:57, 21 s after that turn ended at 09:56:36.

Reading of agent_daemon._tick: one session for the whole tick (get_db), one
commit after the last turn. Once the first goal settles,
_settle_dispatched_goal flushes its writes (db.flush()). From then until the
tick commits, that connection holds SQLite's single write lock. So the next
goal's whole in-process turn runs while the database is locked against every
other connection, including its own fresh sessions (person_ask.ask,
the audit log), which give up after busy_timeout (3 s).

Behavioural: the real AgentDaemon._tick, _settle_dispatched_goal,
person_ask.ask, NotificationService.create, db_session/get_db and the real
engine (file SQLite, WAL, NullPool, busy_timeout) on a temp file. Replaced
at the boundaries only: the idle personas, the yield gate, the recipe check,
the prompt builder, the cost estimate, the guardrail, the fan-out, the hive
check, the Liquid UI push, the live fan-out and the /chat turn (which asks
its person, as the live turn did).
"""
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """The node's real engine and session factory, on a file database."""
    from integrations.social import models
    path = str(tmp_path / 'hevolve_database.db')
    monkeypatch.setattr(models, 'DB_PATH', path)
    monkeypatch.setattr(models, 'DB_URL', f'sqlite:///{path}')
    monkeypatch.setattr(models, '_engine', None)
    monkeypatch.setattr(models, '_SessionLocal', None)
    models.init_db()
    yield models
    eng = models._engine
    if eng is not None:
        eng.dispose()


def _goals(models, n):
    from integrations.social.models import AgentGoal
    ids = []
    with models.db_session() as db:
        for i in range(n):
            gid = f'goal-lock-{i}-{uuid.uuid4().hex[:6]}'
            db.add(AgentGoal(id=gid, goal_type='marketing', title=f'Goal {i}',
                             description='', status='active', config_json={},
                             spark_budget=500, spark_spent=0))
            ids.append(gid)
    return ids


def _tick(models, n_goals):
    """One real tick over ``n_goals`` active goals. Each goal's turn asks
    its person through the real person_ask.ask. Returns what each ask
    returned, in turn order, and the asks the database holds afterwards."""
    from integrations.agent_engine import agent_daemon, dispatch, person_ask
    from integrations.agent_engine import liquid_ui_service as lui
    from integrations.agent_engine.agent_daemon import AgentDaemon
    from integrations.social.models import Notification

    goal_ids = _goals(models, n_goals)
    asked = []

    def _turn(prompt, user_id, goal_id, goal_type, **kw):
        out = person_ask.ask('person-1', 'agent-1',
                             {'type': 'choice', 'title': f'Which one for {goal_id}?',
                              'description': 'The next step needs your choice.'},
                             prompt_id='123')
        asked.append((str(goal_id), out.get('ok'), out.get('refused')))
        return 'The step is done.'

    agents = [{'user_id': f'agent-{i}', 'username': f'a{i}'} for i in range(n_goals)]
    patches = [
        patch('integrations.coding_agent.idle_detection.IdleDetectionService.'
              'get_idle_agent_personas', return_value=agents),
        patch.object(dispatch, 'should_yield_to_user', return_value=False),
        patch.object(dispatch, 'dispatch_goal', side_effect=_turn),
        patch.object(dispatch, 'handed_to_hive', return_value=False),
        # The box's own load would cap the tick at one turn; the test needs
        # the tick to run as many turns as it has goals.
        patch.object(dispatch, 'max_autonomous_concurrency',
                     side_effect=lambda n, *a, **k: n),
        patch('integrations.service_tools.model_lifecycle.'
              'get_model_lifecycle_manager',
              return_value=MagicMock(**{'get_system_pressure.return_value':
                                        {'throttle_factor': 1.0}})),
        patch.object(agent_daemon, '_flow_recipe_exists', return_value=True),
        patch.object(AgentDaemon, '_trends_tick', return_value=None),
        patch.object(AgentDaemon, '_try_parallel_dispatch',
                     return_value={'completed': 0, 'failed': 0, 'deferred': 0}),
        patch('integrations.agent_engine.goal_manager.GoalManager.build_prompt',
              return_value='Do the next step'),
        patch('integrations.agent_engine.budget_gate.estimate_llm_cost_spark',
              return_value=0),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda prompt, *a, **k: (True, '', prompt)),
        patch('security.immutable_audit_log.get_audit_log',
              return_value=MagicMock()),
        patch.object(lui, 'push_agent_ui', return_value=True),
        patch.object(lui, 'withdraw_agent_ui', return_value=None),
        patch('integrations.social.realtime.on_notification', return_value=None),
    ]
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            AgentDaemon()._tick()
        finally:
            with person_ask._lock:
                person_ask._asks.clear()
            for gid in goal_ids:
                agent_daemon._dispatch_backoff.pop(gid, None)
                agent_daemon._budget_blocked_goals.discard(gid)

    with models.db_session(commit=False) as db:
        filed = db.query(Notification).filter_by(type=person_ask.ASK_TYPE).count()
    return asked, filed


def test_the_second_goals_ask_is_filed(file_db):
    """FAILS BEFORE THE FIX: the second turn's ask waited 3 s on the lock the
    tick took when it settled the first goal, and came back 'not_filed'."""
    asked, filed = _tick(file_db, 2)
    assert len(asked) == 2, asked
    assert [ok for _, ok, _ in asked] == [True, True], asked
    assert filed == 2, f'{filed} of 2 asks reached the database: {asked}'


def test_a_lone_goals_ask_is_filed(file_db):
    """The first turn of a tick was never blocked. It stays filed."""
    asked, filed = _tick(file_db, 1)
    assert [ok for _, ok, _ in asked] == [True], asked
    assert filed == 1


def test_the_tick_still_records_its_dispatch_stamps(file_db):
    """The stamps the tick sets before each turn reach the database, for
    every goal it ran (last_dispatched_at; spark_at_dispatch in config)."""
    from integrations.social.models import AgentGoal
    _tick(file_db, 2)
    with file_db.db_session(commit=False) as db:
        rows = db.query(AgentGoal).all()
        stamped = [(r.last_dispatched_at is not None,
                    'spark_at_dispatch' in (r.config_json or {})) for r in rows]
    assert stamped == [(True, True), (True, True)], stamped
