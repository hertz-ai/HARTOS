"""A goal the speculative dispatcher ran is judged by the reply its turn gave.

Measured on central 2026-10-17 18:35-18:49Z (container c48e9060):
- Once the Claude key made a second dispatchable model exist,
  should_speculate turned true and every daemon goal took the speculative
  branch (HEVOLVE_SPECULATIVE_ENABLED is set true by the node-tier feature
  gate).  dispatch_speculative runs the fast model's /chat turn
  synchronously and returns its reply; the branch threw the reply away and
  set result to the 'speculative-handoff' sentinel.
- Every fast turn failed (OpenRouter 400 "Reasoning is mandatory"), and
  18 goals were still completed, one every ~42 s, 1.5 s after their
  dispatch: the expert reservation inside dispatch_speculative added 4 spark
  (spark_this_dispatch=4), and the coordinator still held the task the
  morning's false result had completed (grounding ledger_tasks_complete),
  or no task at all (local_dispatch_spend).

The reply is the turn's result.  A failed or empty one is a failed dispatch
(backoff, the 5-strike pause with its reason); a paused one is held; a real
one is settled like any other turn that ran here.

Behavioural: the real AgentDaemon._tick, dispatch's failure rule and
_settle_dispatched_goal.  Replaced at the boundaries only: the DB session and
goal row, the idle personas, the yield and person checks, the budget and
guardrail gates, the prompt builder, the coordinator's accounting and the
speculative dispatcher (its fast turn and its reservation).

    python -m pytest tests/unit/test_speculative_turn_is_judged_by_its_reply.py -q
"""
import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.agent_engine import agent_daemon, budget_gate, dispatch  # noqa: E402
from integrations.agent_engine.agent_daemon import AgentDaemon  # noqa: E402

GOAL = 'goal-spec-1'
FAILED = ("I couldn't finish that: Error code: 400 - {'error': {'message': "
          "'Reasoning is mandatory for this endpoint and cannot be disabled.', "
          "'code': 400}}")


class Goal:
    """Stands in for an AgentGoal row (the attributes _tick reads)."""

    def __init__(self, gid=GOAL, spark=104):
        self.id = gid
        self.goal_type = 'marketing'
        self.title = 'Growth Analytics and Optimization'
        self.description = ''
        self.status = 'active'
        self.config_json = {'bootstrap_slug': 'bootstrap_growth_analytics'}
        self.spark_spent = spark
        self.spark_budget = 300
        self.last_dispatched_at = None
        self.product_id = None
        self.owner_id = None
        self.prompt_id = None
        self.flow_id = None

    def to_dict(self):
        d = {'id': self.id, 'goal_type': self.goal_type, 'title': self.title,
             'description': self.description, 'status': self.status,
             'spark_budget': self.spark_budget, 'spark_spent': self.spark_spent}
        d.update(self.config_json)
        return d


class _Coordinator:
    """The coordinator's own accounting for the goal: the task an earlier
    run left behind (completed), or none at all."""

    def __init__(self, progress):
        self._progress = progress

    def get_goal_progress(self, goal_id):
        return self._progress


STALE_DONE = {'total_tasks': 1, 'completed': 1}


class _Speculator:
    """The speculative dispatcher at its boundary: the fast turn's reply, and
    the expert reservation that adds spark before any work exists
    (budget_gate.check_goal_budget, called from _check_and_reserve_budget)."""

    def __init__(self, goal, reply, reserve=4, work=0):
        self.goal = goal
        self.reply = reply
        self.reserve = reserve
        self.work = work
        self.calls = 0

    def should_speculate(self, *a, **k):
        return True

    def dispatch_speculative(self, prompt, user_id, prompt_id=None,
                             goal_id=None, goal_type='general', node_id=None):
        self.calls += 1
        # The expert reservation, through the real gate
        # (_schedule_expert_background -> check_goal_budget).
        budget_gate.check_goal_budget(str(goal_id), self.reserve)
        if self.work:
            # A flow that finished during the fast turn charges its work
            # (create_recipe._save_flow_recipe / reuse_recipe all-done).
            budget_gate.charge_goal_work_completed(prompt_id, self.work)
        return {'response': self.reply, 'speculation_id': 'spec-1',
                'fast_model': 'configured-api', 'expert_pending': True}


def _boundaries(goal, speculator, progress, speculative=True):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [goal]
    # budget_gate's row lookups (by id; by prompt id + status) find the goal.
    db.query.return_value.filter_by.return_value.with_for_update \
        .return_value.first.return_value = goal
    db.query.return_value.filter.return_value.with_for_update \
        .return_value.first.return_value = goal
    return [
        patch.dict(os.environ, {'HEVOLVE_SPECULATIVE_ENABLED':
                                'true' if speculative else 'false'}),
        patch('integrations.social.models.get_db', return_value=db),
        patch('integrations.coding_agent.idle_detection.IdleDetectionService.'
              'get_idle_agent_personas',
              return_value=[{'user_id': 'agent-1', 'username': 'a1'}]),
        patch.object(dispatch, 'should_yield_to_user', return_value=False),
        patch.object(dispatch, 'is_user_recently_active', return_value=False),
        patch.object(dispatch, '_cb_is_open', return_value=False),
        patch('core.foreground.preempted_recently', return_value=False),
        patch('integrations.agent_engine.model_registry._own_llm_target',
              return_value=('', '')),
        patch.object(agent_daemon, '_flow_recipe_exists', return_value=True),
        patch('integrations.agent_engine.goal_manager.GoalManager.build_prompt',
              return_value='Analyze platform growth metrics and optimize'),
        patch('integrations.agent_engine.budget_gate.estimate_llm_cost_spark',
              return_value=0),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda prompt, *a, **k: (True, '', prompt)),
        patch.object(AgentDaemon, '_try_parallel_dispatch',
                     return_value={'completed': 0, 'failed': 0, 'deferred': 0}),
        patch.object(dispatch, '_get_distributed_coordinator',
                     return_value=(_Coordinator(progress)
                                   if progress is not None else None)),
        patch('integrations.agent_engine.speculative_dispatcher.'
              'get_speculative_dispatcher', return_value=speculator),
    ]


def tick(goal, speculator, progress=STALE_DONE, speculative=True, extra=()):
    """One real agent-daemon tick over ``goal``; a backoff left by the last
    tick is due now, the count of failures it holds is kept.  Ticks run 30 s
    apart, past the budget cache's 10 s window, so each starts uncached."""
    info = agent_daemon._dispatch_backoff.get(str(goal.id))
    if info:
        info['skip_until'] = 0
    agent_daemon._budget_blocked_goals.discard(str(goal.id))
    budget_gate.invalidate_goal_budget_cache()
    goal.last_dispatched_at = None
    with contextlib.ExitStack() as stack:
        for p in list(_boundaries(goal, speculator, progress,
                                  speculative)) + list(extra):
            stack.enter_context(p)
        AgentDaemon()._tick()


def _forget_held():
    getattr(budget_gate, '_held_reservations', {}).pop(GOAL, None)


@pytest.fixture(autouse=True)
def _fresh_daemon_state():
    agent_daemon._dispatch_backoff.pop(GOAL, None)
    # The route line is logged only when a goal's route changes, so each
    # test starts as if the goal had never been dispatched.
    dispatch._routes_logged.pop(GOAL, None)
    budget_gate.invalidate_goal_budget_cache()
    _forget_held()
    yield
    agent_daemon._dispatch_backoff.pop(GOAL, None)
    dispatch._routes_logged.pop(GOAL, None)
    dispatch.dispatch_failure_reason(GOAL)
    budget_gate.invalidate_goal_budget_cache()
    _forget_held()


def test_a_failed_fast_turn_does_not_complete_the_goal():
    """The 18 completions themselves: a reserved 4 spark and a task an
    earlier run completed, and a turn that answered with its 400."""
    goal = Goal()
    spec = _Speculator(goal, FAILED)
    tick(goal, spec)
    assert spec.calls == 1
    assert goal.status == 'active', \
        'a failed turn completed the goal on a reservation and a stale task'
    assert 'completed_at' not in goal.config_json
    assert agent_daemon._dispatch_backoff[GOAL]['failures'] == 1, \
        'the failed turn must count as a failed dispatch'


def test_five_failed_fast_turns_pause_the_goal_and_say_why():
    goal = Goal()
    spec = _Speculator(goal, FAILED)
    for _ in range(5):
        tick(goal, spec)
    assert spec.calls == 5
    assert goal.status == 'paused'
    reason = goal.config_json['pause_reason']
    assert '5 consecutive dispatch failures' in reason
    assert 'Reasoning is mandatory' in reason, \
        'the pause must name what failed, as a dispatch_goal turn does'


def test_an_empty_fast_reply_is_a_failed_dispatch():
    """dispatch_speculative answers '' when its /chat call fails (a 401, a
    timeout): no turn result, so no settlement."""
    goal = Goal()
    tick(goal, _Speculator(goal, ''))
    assert goal.status == 'active'
    assert 'completed_at' not in goal.config_json
    assert agent_daemon._dispatch_backoff[GOAL]['failures'] == 1


def test_five_empty_fast_replies_pause_the_goal_naming_the_model():
    """_dispatch_to_model swallows the cause of an empty reply, so the pause
    names the model an operator has to look at (review of aeb5d4831)."""
    goal = Goal()
    for _ in range(5):
        tick(goal, _Speculator(goal, ''))
    assert goal.status == 'paused'
    reason = goal.config_json['pause_reason']
    assert 'configured-api' in reason and 'returned no reply' in reason


class _RaisingSpeculator(_Speculator):
    def dispatch_speculative(self, *a, **k):
        self.calls += 1
        raise RuntimeError('fast model transport broke')


def test_a_raising_speculative_dispatch_is_a_failed_turn_not_a_dead_tick():
    """A raise used to end the tick, and the tick's handler clears every
    goal's backoff: the goal never collected a strike and another goal's
    backoff was wiped (review of aeb5d4831, measured)."""
    other = {'failures': 3, 'skip_until': 9e18}
    agent_daemon._dispatch_backoff['goal-other'] = dict(other)
    try:
        goal = Goal()
        spec = _RaisingSpeculator(goal, 'unused')
        tick(goal, spec)
        assert spec.calls == 1
        assert agent_daemon._dispatch_backoff[GOAL]['failures'] == 1
        assert agent_daemon._dispatch_backoff.get('goal-other') == other, \
            "another goal's backoff was cleared by this goal's raise"
        for _ in range(4):
            tick(goal, spec)
        assert goal.status == 'paused'
        assert 'RuntimeError: fast model transport broke' in \
            goal.config_json['pause_reason']
    finally:
        agent_daemon._dispatch_backoff.pop('goal-other', None)


def test_the_turn_says_where_it_ran():
    """The 18 completions left no line saying where their turns went; the
    route is recorded and logged like a dispatch_goal turn's."""
    goal = Goal()
    with patch.object(dispatch.logger, 'info') as info:
        tick(goal, _Speculator(goal, FAILED))
    lines = [str(c.args[0]) for c in info.call_args_list if c.args]
    assert any(f'Goal {GOAL}: dispatched here (the speculative fast model '
               f'ran it)' in line for line in lines), lines


def test_a_fast_turn_paused_for_the_person_is_held_not_failed():
    from core.agent_tools import user_pause_reply
    goal = Goal()
    tick(goal, _Speculator(goal, user_pause_reply(1)))
    assert goal.status == 'active'
    assert 'completed_at' not in goal.config_json
    assert GOAL not in agent_daemon._dispatch_backoff, \
        'a turn paused for the person is no strike against the goal'


REAL = ('Funnel drop-off is at sign-up; three posts drafted for the '
        'student segment.')


def test_a_real_fast_reply_with_no_work_charged_is_a_noop_not_a_completion():
    """The reply reaches the one completion gate (defect (b), 2026-09-02),
    and the gate judges it on work, not on the expert's reservation (review
    of aeb5d4831, finding 1): no flow finished, so nothing was earned."""
    goal = Goal()
    tick(goal, _Speculator(goal, REAL), progress=None)
    assert goal.status == 'active'
    assert 'completed_at' not in goal.config_json
    assert goal.config_json['noop_dispatch_count'] == 1, \
        'the reply reached the gate and was judged a noop'
    assert goal.spark_spent == 104, 'the reservation was given back'
    assert GOAL not in agent_daemon._dispatch_backoff


def test_a_real_fast_reply_whose_flow_finished_completes_the_goal():
    goal = Goal()
    tick(goal, _Speculator(goal, REAL, work=2), progress=None)
    assert goal.status == 'completed'
    assert goal.config_json['completion_grounding'] == 'local_dispatch_spend'
    assert goal.spark_spent == 106, 'only the completed-work charge stays'


def test_a_failed_turn_gives_its_reservation_back():
    """Budgets drained 4 spark per failed dispatch until the budget gate
    paused the goal ('insufficient_budget (0 < 4)', central 10-17)."""
    goal = Goal()
    tick(goal, _Speculator(goal, FAILED))
    assert goal.spark_spent == 104
    assert budget_gate.held_reservation(GOAL) == 0


def test_a_dispatch_goal_turn_gives_its_reservation_back_too():
    """The same for the ordinary path: dispatch_goal's pre_dispatch_budget_gate
    reserves before its turn, and a reply with no completed work is a noop."""
    goal = Goal()

    def _dispatch_goal(prompt, user_id, goal_id, goal_type='marketing',
                       model_config=None):
        budget_gate.check_goal_budget(str(goal_id), 4)
        return REAL

    tick(goal, _Speculator(goal, 'unused'), progress=None, speculative=False,
         extra=[patch.object(dispatch, 'dispatch_goal', _dispatch_goal)])
    assert goal.status == 'active'
    assert goal.config_json['noop_dispatch_count'] == 1
    assert goal.spark_spent == 104
