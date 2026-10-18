"""Spark reserved at dispatch is given back when the dispatch ends.

check_goal_budget reserves the estimated cost by adding it to
goal.spark_spent before the turn runs ("check and reserve").  Nothing gave
it back, so the reservation stood in for work in two places:

- the completion gate reads "spark spent this dispatch" as evidence; on
  central 2026-10-17 18 goals whose every turn failed were completed on the
  4 spark the expert reservation added (review of aeb5d4831, finding 1);
- budgets drained by 4 spark per failed or waiting dispatch until the
  budget gate paused the goal: "insufficient_budget (0 < 4)", five goals on
  central the same evening.

The steward's rule (budget_gate.charge_goal_work_completed, 2026-06-10) is
that spark_spent rises only on work done.  The reservation is a pre-check:
held while the dispatch runs, released when it ends.

Behavioural: the real check_goal_budget, charge_goal_work_completed and
release_goal_reservations against a stand-in session (the DB boundary), as
tests/unit/test_charge_goal_work_completed.py does.

    python -m pytest tests/unit/test_dispatch_reservation_is_given_back.py -q
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import budget_gate  # noqa: E402

GOAL = 'goal-res-1'


class Goal:
    def __init__(self, budget=300, spent=100):
        self.id = GOAL
        self.spark_budget = budget
        self.spark_spent = spent
        self.status = 'active'


def _db(goal):
    """A session whose AgentGoal lookups (by id, and by prompt id with
    status) resolve to ``goal``."""
    db = MagicMock()
    db.query.return_value.filter_by.return_value.with_for_update \
        .return_value.first.return_value = goal
    db.query.return_value.filter.return_value.with_for_update \
        .return_value.first.return_value = goal
    return db


@pytest.fixture(autouse=True)
def _clean():
    budget_gate.invalidate_goal_budget_cache()
    budget_gate._held_reservations.pop(GOAL, None)
    yield
    budget_gate.invalidate_goal_budget_cache()
    budget_gate._held_reservations.pop(GOAL, None)


def test_a_reservation_is_held_until_released():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        allowed, remaining, reason = budget_gate.check_goal_budget(GOAL, 4)
        assert (allowed, reason) == (True, 'budget_reserved')
        assert goal.spark_spent == 104
        assert budget_gate.held_reservation(GOAL) == 4
        released = budget_gate.release_goal_reservations(GOAL)
    assert released == 4
    assert goal.spark_spent == 100, 'the reservation is not work'
    assert budget_gate.held_reservation(GOAL) == 0


def test_work_charged_during_the_dispatch_stays():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        assert budget_gate.charge_goal_work_completed('12345', 2) is True
        budget_gate.release_goal_reservations(GOAL)
    assert goal.spark_spent == 102, 'only the completed-work charge remains'


def test_a_cached_check_reserves_nothing_and_holds_nothing():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        budget_gate.check_goal_budget(GOAL, 4)      # within the cache window
    assert goal.spark_spent == 104
    assert budget_gate.held_reservation(GOAL) == 4


def test_release_never_takes_spent_below_zero():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        goal.spark_spent = 1                         # an operator reset it
        budget_gate.release_goal_reservations(GOAL)
    assert goal.spark_spent == 0


def test_release_clears_the_cache_so_the_next_check_reads_the_row():
    goal = Goal(budget=104, spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)      # leaves 0: cached 'allowed'
        budget_gate.release_goal_reservations(GOAL)
        allowed, remaining, reason = budget_gate.check_goal_budget(GOAL, 4)
    assert (allowed, reason) == (True, 'budget_reserved'), \
        'a stale cached verdict was served after the release'
    assert goal.spark_spent == 104


def test_a_failed_release_keeps_the_hold_for_the_next_one():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    broken = MagicMock()
    broken.query.side_effect = RuntimeError('database is locked')
    with patch('integrations.social.models.get_db', return_value=broken):
        assert budget_gate.release_goal_reservations(GOAL) == 0
    assert budget_gate.held_reservation(GOAL) == 4
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        assert budget_gate.release_goal_reservations(GOAL) == 4
    assert goal.spark_spent == 100


def test_two_releases_at_once_give_the_reservation_back_once():
    """Both read the hold, both wrote it back, and spent dropped below what
    the goal had before the reservation (review of e8b6bf675, P5)."""
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)

    def _slow_row():
        time.sleep(0.3)
        return goal

    slow = MagicMock()
    slow.query.return_value.filter_by.return_value.with_for_update \
        .return_value.first.side_effect = _slow_row
    results = []
    with patch('integrations.social.models.get_db', return_value=slow):
        threads = [threading.Thread(target=lambda: results.append(
            budget_gate.release_goal_reservations(GOAL))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
    assert sorted(results) == [0, 4]
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


def test_a_hold_whose_goal_row_is_gone_gives_nothing_back():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    with patch('integrations.social.models.get_db', return_value=_db(None)):
        assert budget_gate.release_goal_reservations(GOAL) == 0
    assert budget_gate.held_reservation(GOAL) == 0, \
        'a hold on a deleted goal is dropped, not retried forever'


def test_spark_spent_on_work_leaves_the_held_reservation_out():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    assert goal.spark_spent == 104
    assert budget_gate.spark_spent_on_work(GOAL, goal.spark_spent) == 100
    assert budget_gate.spark_spent_on_work(GOAL, None) == 0
    assert budget_gate.spark_spent_on_work('nothing-held', 7) == 7


def test_nothing_held_releases_nothing():
    broken = MagicMock()
    broken.query.side_effect = AssertionError('must not touch the database')
    with patch('integrations.social.models.get_db', return_value=broken):
        assert budget_gate.release_goal_reservations(GOAL) == 0


# ── Each reserver gives back its own (review of e8b6bf675, Q4 + P6) ──
#
# A goal-wide release freed whatever was held for the goal: the daemon gave
# back the speculative expert's reservation while the expert still ran, and
# any release could give back another caller's.  Each reservation now has a
# holder, and the code that reserved it releases it.

def test_releasing_one_holder_leaves_another_holders_reservation():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4, holder='expert:s1')
        budget_gate.invalidate_goal_budget_cache()
        budget_gate.check_goal_budget(GOAL, 3, holder='dispatch:d1')
        assert goal.spark_spent == 107
        assert budget_gate.held_reservation(GOAL) == 7
        assert budget_gate.release_goal_reservations(
            GOAL, holder='dispatch:d1') == 3
        assert goal.spark_spent == 104
        assert budget_gate.held_reservation(GOAL) == 4, \
            "the expert's reservation was given back by another holder"
        assert budget_gate.release_goal_reservations(
            GOAL, holder='expert:s1') == 4
    assert goal.spark_spent == 100


FAILED = ("I couldn't finish that: Error code: 400 - {'error': {'message': "
          "'Reasoning is mandatory for this endpoint and cannot be disabled.', "
          "'code': 400}}")


def _dispatch_goal_boundaries(goal, turn, allowed=True, affordable=True):
    """The real dispatch_goal and the real budget gate, replaced at the
    boundaries: the goal row, the cost estimate, the platform's revenue, the
    model target, the coordinator, the guardrail, the audit log and the
    in-process /chat turn."""
    from integrations.agent_engine import dispatch
    return [
        patch('integrations.social.models.get_db', return_value=_db(goal)),
        patch.object(budget_gate, 'estimate_llm_cost_spark', return_value=4),
        patch.object(budget_gate, 'check_platform_affordability',
                     return_value=(affordable, {'net_7d': 0 if affordable
                                                else -5})),
        patch('integrations.agent_engine.model_registry._own_llm_target',
              return_value=('', '')),
        patch.object(dispatch, '_get_distributed_coordinator',
                     return_value=None),
        patch.object(dispatch, 'local_chat_dispatch', **turn),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda p, *a, **k: (allowed, '' if allowed
                                              else 'blocked', p)),
        patch('security.immutable_audit_log.get_audit_log'),
    ]


def _dispatch_goal(goal, turn, **kw):
    import contextlib
    from integrations.agent_engine import dispatch
    with contextlib.ExitStack() as stack:
        for p in _dispatch_goal_boundaries(goal, turn, **kw):
            stack.enter_context(p)
        return dispatch.dispatch_goal('Draft three posts for students',
                                      'agent-1', GOAL, 'marketing')


@pytest.mark.parametrize('turn, expected', [
    ({'return_value': ('ok', 'Drafted three posts.')}, 'Drafted three posts.'),
    ({'return_value': ('ok', FAILED)}, None),
    ({'return_value': ('deferred', None)}, None),
], ids=['answered', 'failed', 'deferred'])
def test_dispatch_goal_gives_back_its_reservation_however_the_turn_ends(
        turn, expected):
    """The stdio MCP server (mcp_server.py) and every other caller of
    dispatch_goal outside the daemons reserved and never gave back."""
    goal = Goal(spent=100)
    assert _dispatch_goal(goal, turn) == expected
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


def test_dispatch_goal_gives_back_its_reservation_when_the_turn_raises():
    goal = Goal(spent=100)
    with pytest.raises(RuntimeError):
        _dispatch_goal(goal, {'side_effect': RuntimeError('adapter broke')})
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


@pytest.mark.parametrize('kw', [{'allowed': False}, {'affordable': False}],
                         ids=['guardrail-refused', 'platform-unaffordable'])
def test_a_dispatch_refused_after_its_reservation_gives_it_back(kw):
    """The platform check runs after the goal's spark is reserved, and the
    guardrail after the budget gate: both refusals left the spark held."""
    goal = Goal(spent=100)
    assert _dispatch_goal(goal, {'return_value': ('ok', 'unused')}, **kw) \
        is None
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


def test_dispatch_goal_leaves_a_running_experts_reservation_alone():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4, holder='expert:s1')
    budget_gate.invalidate_goal_budget_cache()
    _dispatch_goal(goal, {'return_value': ('ok', 'Drafted three posts.')})
    assert goal.spark_spent == 104
    assert budget_gate.held_reservation(GOAL) == 4


class _Model:
    def __init__(self, model_id, cost):
        self.model_id = model_id
        self.cost_per_1k_tokens = cost


class _Registry:
    """The model registry at its boundary: a free local fast model and a
    priced expert."""
    fast = _Model('fast-local', 0)
    expert = _Model('claude-expert', 4)

    def get_fast_model(self):
        return self.fast

    def get_expert_model(self):
        return self.expert

    def speculation_pair(self):
        return self.fast, self.expert

    def record_energy(self, *a, **k):
        pass

    def record_latency(self, *a, **k):
        pass

    def get_total_energy_kwh(self, hours=0):
        return 0.0


class _Pool:
    """The expert pool, holding each submitted task until the test runs it."""

    def __init__(self, fail=False):
        self.jobs = []
        self.fail = fail

    def submit(self, fn, *a, **k):
        if self.fail:
            raise RuntimeError('cannot schedule new futures after shutdown')
        self.jobs.append((fn, a, k))

    def run(self):
        for fn, a, k in self.jobs:
            fn(*a, **k)


def _speculative_dispatcher(pool=None):
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    d = SpeculativeDispatcher(model_registry=_Registry())
    d._expert_pool.shutdown(wait=False)
    d._expert_pool = pool or _Pool()
    return d


def _speculate(goal, d, expert=None):
    import contextlib
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('integrations.social.models.get_db',
                                  return_value=_db(goal)))
        stack.enter_context(patch.object(d, '_dispatch_to_model',
                                         return_value='Fast reply.'))
        stack.enter_context(patch.object(d, '_track_call_telemetry'))
        ran = stack.enter_context(patch.object(
            d, '_run_collapsed_expert_path', **(expert or {})))
        out = d.dispatch_speculative('Analyze platform growth metrics',
                                     'agent-1', '12345', GOAL, 'marketing')
        held_while_pending = (goal.spark_spent,
                              budget_gate.held_reservation(GOAL))
        d._expert_pool.run()
        return out, held_while_pending, ran


def test_the_expert_holds_its_reservation_until_its_task_ends():
    goal = Goal(spent=100)
    out, pending, ran = _speculate(goal, _speculative_dispatcher())
    assert out['expert_pending'] is True
    assert pending == (104, 4), 'the expert has not run yet'
    ran.assert_called_once()
    assert goal.spark_spent == 100, 'the expert gave its reservation back'
    assert budget_gate.held_reservation(GOAL) == 0


def test_an_expert_that_raises_still_gives_its_reservation_back():
    goal = Goal(spent=100)
    _speculate(goal, _speculative_dispatcher(),
               expert={'side_effect': RuntimeError('expert transport broke')})
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


def test_an_expert_that_cannot_be_scheduled_gives_its_reservation_back():
    goal = Goal(spent=100)
    with pytest.raises(RuntimeError):
        _speculate(goal, _speculative_dispatcher(pool=_Pool(fail=True)))
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0
