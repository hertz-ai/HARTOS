"""A goal the daemon auto-paused for consecutive dispatch failures is resumed
by the daemon once the cause has gone, instead of staying paused until a
person finds the dashboard.

Measured on central 2026-10-28 (owner /goal, 23:5x IST): every LLM turn from
10-25 answered one 400 ('Reasoning is mandatory for this endpoint and cannot
be disabled.'); 17 goals auto-paused that day on '5 consecutive dispatch
failures', 70 paused / 4 active, 0 dispatches in 24 h, and the only resume
path was dashboard_service's steer.  The reasoning fix (3c126fd24) lands
with a deploy; nothing then resumes those goals, so the daemon stays idle
with its goals in hand.

The rule pinned here, all inside agent_daemon (no new module, no second
status writer):
  * only a goal paused for 'consecutive dispatch failures' is eligible (a
    goal paused for ledger grounding, budget, consent or a person's help
    has a different cause and keeps its pause);
  * a successful dispatch on this node is the evidence the cause has gone:
    it releases up to three such goals, oldest pause first;
  * with nothing succeeding, one goal is re-tried per half hour as a probe,
    and only while no active goal is still failing (the dispatch backoff
    table is the existing record of that);
  * at most three resumes per goal; after that it stays paused for a person
    (the pause record already tells them);
  * the status moves only through GoalManager.update_goal_status, the one
    writer /api/goals and the dashboard steer use, and the goal's backoff
    entry is cleared so the retry starts at zero failures;
  * the goal's config keeps the history (last_pause_reason, resume_count,
    resumed_at, resumed_by) as a NEW dict, so the JSON column sees it.
"""
import inspect
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import integrations.agent_engine.agent_daemon as ad  # noqa: E402
from integrations.agent_engine.agent_daemon import (  # noqa: E402
    _resume_goals_paused_for_a_cleared_cause)

FAILURE_REASON = ("Auto-paused: 5 consecutive dispatch failures (last: turn "
                  "failed: I couldn't finish that: Error code: 400 - "
                  "{'error': {'message': 'Reasoning is mandatory'}})")
LEDGER_REASON = ("Auto-paused: work was dispatched and spark was spent, but "
                 "the ledger never reported every task done after 2323s.")
HELP_REASON = "Needs help: action 2 (google_search) could not be done autonomously"


class Goal:
    def __init__(self, gid, reason, paused_at, resume_count=None, status='paused'):
        self.id = gid
        self.goal_type = 'marketing'
        self.status = status
        cfg = {'pause_reason': reason, 'paused_at': paused_at.isoformat()}
        if resume_count is not None:
            cfg['resume_count'] = resume_count
        self.config_json = cfg


class Db:
    """Only what the helper may use: the paused rows, through query/filter/all."""

    def __init__(self, goals):
        self._goals = goals

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return [g for g in self._goals if g.status == 'paused']


def _writer_calls():
    """Patch the ONE status writer; it is the only way a status may change."""
    calls = []

    def fake(db, goal_id, status):
        for g in db._goals:
            if g.id == goal_id:
                g.status = status
                calls.append((goal_id, status))
                return {'success': True}
        return {'success': False, 'error': 'Goal not found'}

    return calls, patch('integrations.agent_engine.goal_manager.GoalManager.'
                        'update_goal_status', side_effect=fake)


def _fresh_module_state():
    ad._dispatch_backoff.clear()
    ad._last_probe_resume_at = 0.0


def test_a_success_releases_the_three_oldest_failure_paused_goals():
    _fresh_module_state()
    t0 = datetime(2026, 10, 25, 10, 0)
    goals = [
        Goal('g-new', FAILURE_REASON, t0 + timedelta(hours=3)),
        Goal('g-old', FAILURE_REASON, t0),
        Goal('g-mid', FAILURE_REASON, t0 + timedelta(hours=1)),
        Goal('g-ledger', LEDGER_REASON, t0),
        Goal('g-help', HELP_REASON, t0),
        Goal('g-late', FAILURE_REASON, t0 + timedelta(hours=5)),
    ]
    ad._dispatch_backoff['g-old'] = {'failures': 5}
    calls, writer = _writer_calls()
    with writer:
        resumed = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=True)
    assert resumed == ['g-old', 'g-mid', 'g-new']
    assert calls == [('g-old', 'active'), ('g-mid', 'active'), ('g-new', 'active')]
    by_id = {g.id: g for g in goals}
    assert by_id['g-ledger'].status == 'paused'
    assert by_id['g-help'].status == 'paused'
    assert by_id['g-late'].status == 'paused'
    cfg = by_id['g-old'].config_json
    assert cfg['resumed_by'] == 'daemon:cause_cleared'
    assert cfg['resume_count'] == 1
    assert cfg['last_pause_reason'] == FAILURE_REASON
    assert 'pause_reason' not in cfg and 'paused_at' not in cfg
    assert cfg['resumed_at']
    assert 'g-old' not in ad._dispatch_backoff


def test_the_resume_cap_keeps_a_goal_paused_for_a_person():
    _fresh_module_state()
    t0 = datetime(2026, 10, 25, 10, 0)
    goals = [Goal('g-capped', FAILURE_REASON, t0, resume_count=3),
             Goal('g-second', FAILURE_REASON, t0, resume_count=2)]
    calls, writer = _writer_calls()
    with writer:
        resumed = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=True)
    assert resumed == ['g-second']
    assert goals[0].status == 'paused'
    assert goals[1].config_json['resume_count'] == 3


def test_no_probe_while_an_active_goal_is_still_failing():
    _fresh_module_state()
    ad._dispatch_backoff['g-active'] = {'failures': 2}
    goals = [Goal('g-paused', FAILURE_REASON, datetime(2026, 10, 25, 10, 0))]
    calls, writer = _writer_calls()
    with writer:
        resumed = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=False)
    assert resumed == [] and calls == [] and goals[0].status == 'paused'


def test_the_probe_retries_one_goal_per_interval():
    _fresh_module_state()
    t0 = datetime(2026, 10, 25, 10, 0)
    goals = [Goal('g-a', FAILURE_REASON, t0 + timedelta(hours=1)),
             Goal('g-b', FAILURE_REASON, t0)]
    calls, writer = _writer_calls()
    with writer:
        first = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=False)
        again = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=False)
        ad._last_probe_resume_at -= ad._RESUME_RETRY_INTERVAL_S + 1
        later = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=False)
    assert first == ['g-b'] and again == [] and later == ['g-a']
    assert goals[1].config_json['resumed_by'] == 'daemon:retry_probe'
    assert ad._RESUME_RETRY_INTERVAL_S >= 1800


def test_a_writer_refusal_leaves_the_goal_as_it_was():
    _fresh_module_state()
    goals = [Goal('g-x', FAILURE_REASON, datetime(2026, 10, 25, 10, 0))]
    before = dict(goals[0].config_json)
    with patch('integrations.agent_engine.goal_manager.GoalManager.update_goal_status',
               return_value={'success': False, 'error': 'Goal not found'}):
        resumed = _resume_goals_paused_for_a_cleared_cause(Db(goals), cause_cleared=True)
    assert resumed == []
    assert goals[0].status == 'paused'
    assert goals[0].config_json == before


def test_the_tick_resumes_in_both_places():
    """Wiring: the probe runs before the active-goal query (so an all-paused
    node still re-tries), and a dispatch success releases goals right after
    it clears its own backoff."""
    src = inspect.getsource(ad.AgentDaemon._tick)
    probe = src.index('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)')
    query = src.index("AgentGoal.status == 'active'")
    assert probe < query
    success = src.index('# Success — clear backoff')
    release = src.index('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)')
    assert success < release < src.index('_settle_dispatched_goal(db, goal, goal_key', success)
