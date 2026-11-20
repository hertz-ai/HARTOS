"""A goal whose hive work waits on its person is not a stalled hive run.

Measured on central 2026-11-19 (build 58419ffc2): goals fb84cdce (textile
supplier sourcing) and 5ba50f98 (rice order desk) were handed to the hive.
Central's own worker claimed each task about once a minute and held it each
time with the reply "Waiting for the person: Your plan for ... is waiting on
the card: approve it to build it".  Every tick the daemon dispatched the goal
again, which put the held task back to PENDING (_release_held_tasks) before
the settle read the ledger.  After about 1810 s both goals were paused with
cause ledger_grounding: "work was handed to the hive, but the ledger never
reported every task done ... Check the distributed worker on this node".
That was false: the work waited on the owner's approval card, as designed
(owner 11-02: an agent not built goes through the create review stage, with
its questions sent to the owner).

A turn that waits for the person keeps its goal active with no strike
(core.agent_tools.is_waiting_for_person).  The hive leg now does the same:
the grounding wait does not count the time the goal's hive work sat held for
help, just as it does not count the time a refusing provider held it.  A
task that is really lost still pauses the goal.

Behavioural: the real DistributedTaskCoordinator over a real SmartLedger (in
memory) and InMemoryTaskLock, with its submit_goal, claim_next_task and
hold_task; the real agent_daemon._settle_dispatched_goal and
GoalManager.pause_goal.  Replaced at the boundaries only: the goal row (a
stand-in), the DB session, the provider check (dispatch.goal_turn_held: not
held) and the ledger's clock (simulated, so time passes).

    python -m pytest tests/unit/test_a_hive_goal_waiting_on_its_person_is_not_a_stall.py -q
"""
import datetime as _dt
import logging
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from agent_ledger import core as ledger_core  # noqa: E402
from agent_ledger.core import SmartLedger  # noqa: E402
from core.constants import WAITING_FOR_PERSON_REPLY_PREFIX  # noqa: E402
from integrations.agent_engine import agent_daemon, dispatch  # noqa: E402
from integrations.distributed_agent import task_coordinator as tc  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from tests.unit.test_speculative_turn_is_judged_by_its_reply import Goal  # noqa: E402

GOAL = 'fb84cdce-textile-sourcing'
TASK = f'{GOAL}_task_0'
NODE = 'node-central'
PROMPT = 'Source textile suppliers in Tamil Nadu for the export desk'
#: The reply central's worker held the task with (hart_intelligence_entry
#: ._held_plan_turn, a background turn: _plan_wait_reply's prefix).
WAITING = (f'{WAITING_FOR_PERSON_REPLY_PREFIX} Your plan for '
           f'tamil-nadu-textile-supplier-sourcing is waiting on the card: '
           f'approve it to build it, or deny it to drop it.')
TICK_S = 60


class _Mem:
    """The ledger's backend, in memory."""

    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


class _Db:
    def refresh(self, *a, **k):
        pass

    def flush(self):
        pass


def _clock():
    """A datetime whose now() is a simulated clock (``.offset`` seconds)."""

    class _SimDT(_dt.datetime):
        offset = 0.0

        @classmethod
        def now(cls, tz=None):
            return (_dt.datetime(2026, 11, 19, 16, 20, 0)
                    + _dt.timedelta(seconds=cls.offset))
    return _SimDT


def _back(cfg, seconds):
    """Every stamp of the daemon's wait ``seconds`` older: time passing."""
    for key in ('awaiting_verification_since', 'last_verification_check'):
        if cfg.get(key):
            cfg[key] = (datetime.fromisoformat(cfg[key])
                        - timedelta(seconds=seconds)).isoformat()


def _coordinator():
    led = SmartLedger(agent_id='coord', session_id='s', backend=_Mem())
    return tc.DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(), verifier=MagicMock(),
        baseline=MagicMock())


def _dispatch(co):
    """The daemon's hive handoff of the goal (dispatch_goal_distributed's
    submit): the first one makes the task set, every later one reuses it
    and releases a task held for help."""
    co.submit_goal(PROMPT[:200],
                   [{'task_id': TASK, 'description': PROMPT, 'capabilities': []}],
                   {'goal_type': 'marketing', 'user_id': 'h1', 'prompt': PROMPT,
                    'source_node': NODE},
                   goal_id=GOAL)


def _run(goal, co, total_s, worker):
    """``total_s`` of daemon ticks, one every TICK_S: the hive handoff, the
    settle, then the worker's part for that tick (``worker(tick, co)``).
    Returns the tick the goal stopped being active at, or None."""
    sim = _clock()
    with patch.object(tc, 'datetime', sim), \
            patch.object(ledger_core, 'datetime', sim), \
            patch.object(dispatch, '_get_distributed_coordinator',
                         return_value=co), \
            patch.object(dispatch, 'goal_turn_held', return_value=''):
        for tick in range(total_s // TICK_S + 1):
            _dispatch(co)
            agent_daemon._settle_dispatched_goal(_Db(), goal, GOAL,
                                                 handed_to_hive=True)
            if goal.status != 'active':
                return tick
            worker(tick, co)
            _back(goal.config_json, TICK_S)
            sim.offset += TICK_S
    return None


def _holds_on_the_card(tick, co):
    """Central's worker: claims the released task and holds it, its turn
    answering that the plan waits on the owner's card."""
    task = co.claim_next_task(NODE, [])
    assert task is not None, f'tick {tick}: the held task was not released'
    assert co.hold_task(task.task_id, NODE, WAITING)


@pytest.fixture
def goal(monkeypatch):
    monkeypatch.setenv('HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800')
    g = Goal(gid=GOAL)
    g.title = 'Tamil Nadu textile supplier sourcing'
    return g


def test_a_goal_whose_hive_task_waits_on_the_persons_card_is_not_paused(
        goal, caplog):
    """FAILS BEFORE THE FIX: paused at about 1800 s with cause
    ledger_grounding, the two central goals of 19 Nov."""
    co = _coordinator()
    with caplog.at_level(logging.INFO):
        stopped = _run(goal, co, 2400, _holds_on_the_card)
    cfg = goal.config_json
    assert stopped is None and goal.status == 'active', (
        cfg.get('pause_cause'), cfg.get('pause_reason'))
    assert 'pause_cause' not in cfg and 'pause_reason' not in cfg
    assert 'awaiting_verification_since' in cfg, 'its hive run is still settled'
    said = [r.getMessage() for r in caplog.records
            if r.module == 'agent_daemon' and GOAL in r.getMessage()
            and WAITING in r.getMessage()]
    assert said, 'the daemon never said what the goal waits on'


def test_a_task_nobody_runs_still_pauses_as_ledger_grounding(goal):
    """No worker claims the task: the hive run is lost, and the pause and
    its words stand as before."""
    co = _coordinator()
    stopped = _run(goal, co, 2400, lambda tick, co: None)
    assert stopped is not None and goal.status == 'paused'
    assert goal.config_json['pause_cause'] == 'ledger_grounding'
    assert ('ledger never reported every task done'
            in goal.config_json['pause_reason'])


@pytest.mark.parametrize('left', ['pending', 'in_progress'])
def test_a_task_stuck_after_one_hold_still_pauses(goal, left):
    """The worker held the task once, then went silent: the task sits
    PENDING after its release, or a claimed turn never ends.  Its wait for
    the person ended with the release, so the time after it counts, and the
    goal pauses as ledger_grounding within a tick or two of the window."""
    def _once(tick, co):
        if tick == 0:
            _holds_on_the_card(tick, co)
        elif tick == 1 and left == 'in_progress':
            assert co.claim_next_task(NODE, []) is not None
    co = _coordinator()
    stopped = _run(goal, co, 2400, _once)
    assert stopped is not None and goal.status == 'paused'
    assert goal.config_json['pause_cause'] == 'ledger_grounding'
    assert stopped * TICK_S <= 1800 + 3 * TICK_S, stopped
