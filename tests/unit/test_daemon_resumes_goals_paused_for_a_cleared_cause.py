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

Driven against a REAL SQLite AgentGoal and the REAL GoalManager writers
(review of 26b5f8177: a fake Db and a patched writer pinned none of the
findings), so a resume that is flushed but never committed, or a status that
moves without the one writer, shows here.

The rule pinned, all inside agent_daemon and goal_manager (no new module):
  * eligibility is the structured record cfg['pause_cause'] ==
    'dispatch_failures', written by the daemon's own pause; a person's
    dashboard pause ('person'), a ledger-grounding, budget or needs-help
    pause, and a coding goal (coding_daemon's table rows) keep their pause;
    a row paused before the key existed is taken on its prose only while
    nothing has resumed it since;
  * a successful dispatch on this node releases up to three, oldest first;
  * with nothing succeeding, one goal is re-tried per half hour as a probe,
    only while no ACTIVE goal is still failing; a paused goal's own stale
    backoff entry never blocks it (26b5f8177 finding 1), and the pause site
    ends the strike count;
  * three daemon resumes per pause episode, reset when the goal runs again;
  * the resume is GoalManager.resume_goal (the dashboard's too), the backoff
    entry is cleared, the write is COMMITTED by the helper, and a refused
    write leaves the goal exactly as it was.
"""
import inspect
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from integrations.social.models import AgentGoal, Base  # noqa: E402
import integrations.agent_engine.agent_daemon as ad  # noqa: E402
from integrations.agent_engine.agent_daemon import (  # noqa: E402
    PAUSE_CAUSE_DISPATCH_FAILURES, _auto_pause_for_dispatch_failures,
    _resume_goals_paused_for_a_cleared_cause)
from integrations.agent_engine.goal_manager import GoalManager  # noqa: E402

PROSE = ("Auto-paused: 5 consecutive dispatch failures (last: turn failed: "
         "I couldn't finish that: Error code: 400 - Reasoning is mandatory)")
LEDGER = ("Auto-paused: work was dispatched and spark was spent, but the "
          "ledger never reported every task done after 2323s.")
T0 = datetime(2026, 10, 25, 10, 0)


@pytest.fixture
def session_factory():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    ad._dispatch_backoff.clear()
    ad._last_probe_resume_at = 0.0
    yield sessionmaker(bind=engine)
    ad._dispatch_backoff.clear()
    ad._last_probe_resume_at = 0.0


def _goal(db, gid, cfg, goal_type='marketing', status='paused'):
    db.add(AgentGoal(id=gid, goal_type=goal_type, title=gid, status=status,
                     config_json=cfg))
    db.commit()


def _daemon_paused(at, **extra):
    cfg = {'pause_reason': PROSE, 'paused_at': at.isoformat(),
           'pause_cause': PAUSE_CAUSE_DISPATCH_FAILURES, 'paused_by': 'daemon'}
    cfg.update(extra)
    return cfg


def _status(factory, gid):
    db = factory()
    try:
        return db.get(AgentGoal, gid).status
    finally:
        db.close()


def test_a_success_releases_the_three_oldest_daemon_paused_goals(session_factory):
    db = session_factory()
    _goal(db, 'g-new', _daemon_paused(T0 + timedelta(hours=3)))
    _goal(db, 'g-old', _daemon_paused(T0))
    _goal(db, 'g-mid', _daemon_paused(T0 + timedelta(hours=1)))
    _goal(db, 'g-late', _daemon_paused(T0 + timedelta(hours=5)))
    _goal(db, 'g-ledger', {'pause_reason': LEDGER, 'paused_at': T0.isoformat()})
    _goal(db, 'g-person', {'pause_reason': PROSE, 'paused_at': T0.isoformat(),
                           'pause_cause': 'person', 'paused_by': 'person:owner'})
    _goal(db, 'g-coding', {'pause_reason': PROSE}, goal_type='coding')
    ad._dispatch_backoff['g-old'] = {'failures': 5}

    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)

    assert resumed == ['g-old', 'g-mid', 'g-new']
    for gid in ('g-late', 'g-ledger', 'g-person', 'g-coding'):
        assert _status(session_factory, gid) == 'paused', gid
    # Committed by the helper: a fresh session sees it active.
    assert _status(session_factory, 'g-old') == 'active'
    cfg = session_factory().get(AgentGoal, 'g-old').config_json
    assert cfg['resumed_by'] == 'daemon:cause_cleared'
    assert cfg['daemon_resume_count'] == 1 and cfg['resume_count'] == 1
    assert cfg['last_pause_reason'] == PROSE
    assert cfg['last_pause_cause'] == PAUSE_CAUSE_DISPATCH_FAILURES
    assert not {'pause_reason', 'paused_at', 'pause_cause', 'paused_by'} & set(cfg)
    assert 'g-old' not in ad._dispatch_backoff


def test_a_legacy_prose_row_is_taken_only_while_nothing_resumed_it(session_factory):
    db = session_factory()
    _goal(db, 'g-legacy', {'pause_reason': PROSE, 'paused_at': T0.isoformat()})
    _goal(db, 'g-resumed-once', {'pause_reason': PROSE, 'paused_at': T0.isoformat(),
                                 'resume_count': 1})
    for gid in ('g-legacy', 'g-resumed-once'):
        db.get(AgentGoal, gid).last_dispatched_at = T0 - timedelta(minutes=1)
    db.commit()
    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)
    assert resumed == ['g-legacy']
    assert _status(session_factory, 'g-resumed-once') == 'paused'


def test_a_persons_pause_after_a_daemon_resume_is_never_undone(session_factory):
    """Finding 2: auto-pause, person resumes, person pauses, daemon leaves it."""
    from integrations.social.dashboard_service import _write_goal_status
    db = session_factory()
    _goal(db, 'g-p', _daemon_paused(T0))
    goal = db.get(AgentGoal, 'g-p')
    _write_goal_status(db, goal, 'active', actor_id='owner')
    db.commit()
    assert goal.config_json['resumed_by'] == 'person:owner'
    _write_goal_status(db, goal, 'paused', actor_id='owner')
    db.commit()
    assert goal.config_json['pause_cause'] == 'person'
    ad._last_probe_resume_at = 0.0
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True) == []
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False) == []
    assert _status(session_factory, 'g-p') == 'paused'


def test_the_cap_counts_daemon_resumes_per_episode(session_factory):
    db = session_factory()
    _goal(db, 'g-capped', _daemon_paused(T0, daemon_resume_count=3))
    _goal(db, 'g-two', _daemon_paused(T0 + timedelta(hours=1), daemon_resume_count=2))
    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)
    assert resumed == ['g-two']
    assert _status(session_factory, 'g-capped') == 'paused'
    assert session_factory().get(AgentGoal, 'g-two').config_json['daemon_resume_count'] == 3


def test_the_probe_ignores_a_paused_goals_own_stale_strikes(session_factory):
    """Finding 1: the entry the 5th failure left behind must not block it."""
    db = session_factory()
    _goal(db, 'g-stale', _daemon_paused(T0))
    ad._dispatch_backoff['g-stale'] = {'failures': 5, 'skip_until': 0}
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False) == ['g-stale']
    assert _status(session_factory, 'g-stale') == 'active'
    assert session_factory().get(AgentGoal, 'g-stale').config_json['resumed_by'] == 'daemon:retry_probe'


def test_no_probe_while_an_active_goal_is_still_failing(session_factory):
    db = session_factory()
    _goal(db, 'g-active', {}, status='active')
    _goal(db, 'g-paused', _daemon_paused(T0))
    ad._dispatch_backoff['g-active'] = {'failures': 2, 'skip_until': 0}
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False) == []
    assert _status(session_factory, 'g-paused') == 'paused'


def test_the_probe_retries_one_goal_per_interval(session_factory):
    db = session_factory()
    _goal(db, 'g-a', _daemon_paused(T0 + timedelta(hours=1)))
    _goal(db, 'g-b', _daemon_paused(T0))
    first = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)
    again = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)
    ad._last_probe_resume_at -= ad._RESUME_RETRY_INTERVAL_S + 1
    later = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)
    assert (first, again, later) == (['g-b'], [], ['g-a'])
    assert ad._RESUME_RETRY_INTERVAL_S >= 1800


def test_a_refused_write_leaves_the_goal_as_it_was(session_factory):
    db = session_factory()
    _goal(db, 'g-x', _daemon_paused(T0))
    before = dict(db.get(AgentGoal, 'g-x').config_json)
    with patch.object(GoalManager, 'update_goal_status',
                      return_value={'success': False, 'error': 'refused'}):
        assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True) == []
    db.expire_all()
    goal = db.get(AgentGoal, 'g-x')
    assert goal.status == 'paused' and goal.config_json == before


def test_the_pause_site_writes_the_cause_ends_the_strikes_and_asks_at_the_cap(session_factory):
    db = session_factory()
    _goal(db, 'g-fail', {'daemon_resume_count': 3}, status='active')
    goal = db.get(AgentGoal, 'g-fail')
    ad._dispatch_backoff['g-fail'] = {'failures': 5, 'skip_until': 0}
    told = []
    with patch.object(GoalManager, '_tell_its_person',
                      side_effect=lambda db_, g, text: told.append(text) or True):
        _auto_pause_for_dispatch_failures(db, goal, 'g-fail', 5, 'turn failed: 400')
    db.commit()
    cfg = session_factory().get(AgentGoal, 'g-fail').config_json
    assert goal.status == 'paused'
    assert cfg['pause_cause'] == PAUSE_CAUSE_DISPATCH_FAILURES and cfg['paused_by'] == 'daemon'
    assert cfg['pause_reason'].startswith('Auto-paused: 5 consecutive dispatch failures')
    assert 'g-fail' not in ad._dispatch_backoff
    assert len(told) == 1 and 'retried it 3 times' in told[0] and cfg['told_at']


def test_the_pause_site_does_not_ask_before_the_cap(session_factory):
    db = session_factory()
    _goal(db, 'g-first', {}, status='active')
    goal = db.get(AgentGoal, 'g-first')
    with patch.object(GoalManager, '_tell_its_person', return_value=True) as tell:
        _auto_pause_for_dispatch_failures(db, goal, 'g-first', 5, None)
    assert not tell.called and 'told_at' not in goal.config_json


def test_the_tick_wires_the_probe_and_both_successes():
    """Wiring in _tick, anchored on the code it sits next to: the probe runs
    before the active-goal query; both success sites (the plain dispatch and
    the parallel fan-out) release right after they pop their own backoff and
    name the goal that succeeded, so its episode counter is reset by the one
    helper."""
    src = inspect.getsource(ad.AgentDaemon._tick)
    probe = src.index('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)')
    assert probe < src.index("AgentGoal.status == 'active'")
    call = '_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True, succeeded=goal)'
    assert src.count(call) == 2
    pops = [i for i in range(len(src))
            if src.startswith('_dispatch_backoff.pop(goal_key, None)', i)]
    releases = [i for i in range(len(src)) if src.startswith(call, i)]
    for rel in releases:
        assert any(0 < rel - p < 1500 for p in pops)
        assert rel < src.index('_settle_dispatched_goal(db, goal, goal_key', rel)
    assert "daemon_resume_count" not in src        # the reset lives in the helper, once
    assert '_auto_pause_for_dispatch_failures(' in src


def test_a_success_resets_the_episode_counter_of_the_goal_that_ran(session_factory):
    """Finding 6: the retries are spent per pause episode; whichever success
    site ran the goal (plain or fan-out) names it and the helper resets it."""
    db = session_factory()
    _goal(db, 'g-ran', {'daemon_resume_count': 2}, status='active')
    _goal(db, 'g-paused', _daemon_paused(T0))
    ran = db.get(AgentGoal, 'g-ran')
    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True, succeeded=ran)
    assert resumed == ['g-paused']
    assert 'daemon_resume_count' not in session_factory().get(AgentGoal, 'g-ran').config_json


def test_s7_a_goal_the_turn_parked_for_a_person_is_not_paused_as_a_failure(session_factory):
    """hartos-73's S7: the 5th turn asked for help, escalate_goal parked the
    goal for a person; the failure branch then pauses the object it loaded
    before the turn.  The pause site re-reads the goal and leaves a goal that
    is no longer active alone, so the person's hand is not overwritten and
    the daemon never resumes it."""
    from integrations.agent_engine.goal_manager import GoalManager, PAUSE_CAUSE_NEEDS_HELP
    db = session_factory()
    _goal(db, 'g-s7', {'daemon_resume_count': 0}, status='active')
    goal = db.get(AgentGoal, 'g-s7')
    # The turn's help reply, through the one pause routine, as escalate_goal does.
    GoalManager.pause_goal(db, goal, cause=PAUSE_CAUSE_NEEDS_HELP, by='daemon',
                           reason='Needs help: action 3 (google_search) could not be done',
                           extra={'escalation': {'action_id': 3, 'next': 'human'}})
    db.commit()
    ad._dispatch_backoff['g-s7'] = {'failures': 5, 'skip_until': 0}
    _auto_pause_for_dispatch_failures(db, goal, 'g-s7', 5, 'turn failed: help reply')
    db.commit()
    cfg = session_factory().get(AgentGoal, 'g-s7').config_json
    assert cfg['pause_cause'] == PAUSE_CAUSE_NEEDS_HELP
    assert cfg['pause_reason'].startswith('Needs help')
    assert cfg['escalation']['next'] == 'human'
    assert 'g-s7' not in ad._dispatch_backoff               # the strikes still end
    ad._last_probe_resume_at = 0.0
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True) == []
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False) == []


def test_s1_a_daemon_pause_later_parked_for_a_person_is_never_resumed(session_factory):
    """hartos-73's S1: the daemon pauses a goal, then escalate_goal parks it
    for a person (it runs on paused goals, #194).  The person's hand wins:
    neither the success path nor the probe resumes it."""
    from integrations.agent_engine.goal_manager import GoalManager
    db = session_factory()
    _goal(db, 'g-s1', _daemon_paused(T0))
    goal = db.get(AgentGoal, 'g-s1')
    with patch.object(GoalManager, '_escalation_expert', return_value=None), \
            patch.object(GoalManager, '_tell_its_person', return_value=True):
        result = GoalManager.escalate_goal(db, 'g-s1', {
            'action_id': 2, 'action': 'post', 'reason': 'needs a login', 'tried': ['local']})
    assert result.get('stage') == 'human'
    db.commit()
    ad._last_probe_resume_at = 0.0
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True) == []
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False) == []
    assert _status(session_factory, 'g-s1') == 'paused'


def test_a_human_escalation_on_the_record_beats_the_cause(session_factory):
    """Belt and braces for S1/S7: whatever the cause says, a goal carrying a
    human escalation (escalation.next == 'human') is a person's and is never
    resumed by the daemon."""
    db = session_factory()
    _goal(db, 'g-esc', _daemon_paused(T0, escalation={'action_id': 2, 'next': 'human'}))
    _goal(db, 'g-plain', _daemon_paused(T0 + timedelta(hours=1)))
    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)
    assert resumed == ['g-plain']
    assert _status(session_factory, 'g-esc') == 'paused'


def test_a_legacy_prose_row_counts_only_when_the_pause_followed_its_own_dispatch(session_factory):
    """Finding 2, the sounder test for rows paused before the structured
    record existed: the daemon pauses in the tick that dispatched, so its
    pause sits within an hour of the goal's last dispatch; a person's pause
    long after the last dispatch is not taken."""
    db = session_factory()
    _goal(db, 'g-daemon-like', {'pause_reason': PROSE, 'paused_at': (T0 + timedelta(minutes=2)).isoformat()})
    _goal(db, 'g-person-like', {'pause_reason': PROSE, 'paused_at': (T0 + timedelta(days=3)).isoformat()})
    _goal(db, 'g-never-ran', {'pause_reason': PROSE, 'paused_at': T0.isoformat()})
    for gid in ('g-daemon-like', 'g-person-like'):
        db.get(AgentGoal, gid).last_dispatched_at = T0
    db.commit()
    resumed = _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)
    assert resumed == ['g-daemon-like']
    assert _status(session_factory, 'g-person-like') == 'paused'
    assert _status(session_factory, 'g-never-ran') == 'paused'


def _file_db(tmp_path):
    """Two sessions need two transactions: a file database, not StaticPool.
    Returns the session factory and the engine (disposed by the test, so the
    file is closed before pytest removes the directory)."""
    engine = create_engine('sqlite:///' + str(tmp_path / 'two.db').replace('\\', '/'))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _row(factory, gid):
    """The row as committed, read in a session of its own and closed."""
    reader = factory()
    try:
        row = reader.get(AgentGoal, gid)
        return row.status, dict(row.config_json or {}), row.last_dispatched_at
    finally:
        reader.close()


def _tick_stages(db, gid, cfg):
    """The tick's own writes before the turn, as agent_daemon._tick makes
    them: the dispatch stamp and a copy of the pre-turn config carrying this
    tick's spark snapshot."""
    _goal(db, gid, cfg, status='active')
    goal = db.get(AgentGoal, gid)
    goal.last_dispatched_at = T0
    goal.config_json = dict(goal.config_json or {}, spark_at_dispatch=7)
    return goal


def test_s7_two_sessions_the_turns_record_survives_the_failure_arm(tmp_path):
    """Review of fbc54ddd6, critical: the failure arm FLUSHED before it
    refreshed, so the config the tick staged before the turn (its copy of the
    pre-turn row plus spark_at_dispatch) overwrote the record the turn's
    escalate_goal had committed in ITS OWN session: the row read
    status=paused with pause_reason, pause_cause, paused_by, escalation and
    told_at all gone, and the daemon later resumed it as its own.  The
    same-session S7 above never saw it: one session, one object.

    Two sessions on one file database, the tick's real staging and the real
    escalate_goal: the person's record stands, and the tick's own stamp
    (last_dispatched_at) still reaches the row."""
    from integrations.agent_engine.goal_manager import GoalManager, PAUSE_CAUSE_NEEDS_HELP
    factory, engine = _file_db(tmp_path)
    tick = factory()
    goal = _tick_stages(tick, 'g-s7b', {'daemon_resume_count': 0, 'bootstrap_slug': 'guardian'})
    turn = factory()
    with patch.object(GoalManager, '_escalation_expert', return_value=None), \
            patch.object(GoalManager, '_tell_its_person', return_value=True):
        assert GoalManager.escalate_goal(turn, 'g-s7b', {
            'action_id': 3, 'action': 'google_search', 'reason': 'blocked',
            'tried': ['local']}).get('stage') == 'human'
    turn.commit()
    turn.close()
    ad._dispatch_backoff['g-s7b'] = {'failures': 5, 'skip_until': 0}
    _auto_pause_for_dispatch_failures(tick, goal, 'g-s7b', 5, 'turn failed: help reply')
    tick.commit()
    tick.close()
    status, cfg, dispatched_at = _row(factory, 'g-s7b')
    engine.dispose()
    assert status == 'paused'
    assert cfg['pause_cause'] == PAUSE_CAUSE_NEEDS_HELP
    assert cfg['pause_reason'].startswith('Needs help: action 3')
    assert cfg['escalation']['next'] == 'human' and cfg['escalation']['told_at']
    assert cfg['bootstrap_slug'] == 'guardian'
    assert dispatched_at == T0
    assert 'g-s7b' not in ad._dispatch_backoff


def test_the_failure_arm_pauses_the_row_as_committed_plus_the_ticks_own_key(tmp_path):
    """Still active after the failed turn: the daemon's pause is built on the
    row as committed (a key the turn wrote in its own session stands) plus
    the one key this tick owns, spark_at_dispatch, the way the settlement
    gate merges it."""
    factory, engine = _file_db(tmp_path)
    tick = factory()
    goal = _tick_stages(tick, 'g-merge', {'daemon_resume_count': 0})
    turn = factory()
    other = turn.get(AgentGoal, 'g-merge')
    other.config_json = dict(other.config_json or {}, steps_done=2)
    turn.commit()
    turn.close()
    ad._dispatch_backoff['g-merge'] = {'failures': 5, 'skip_until': 0}
    _auto_pause_for_dispatch_failures(tick, goal, 'g-merge', 5, 'turn failed: 400')
    tick.commit()
    tick.close()
    status, cfg, dispatched_at = _row(factory, 'g-merge')
    engine.dispose()
    assert status == 'paused'
    assert cfg['pause_cause'] == PAUSE_CAUSE_DISPATCH_FAILURES
    assert cfg['steps_done'] == 2
    assert cfg['spark_at_dispatch'] == 7
    assert dispatched_at == T0


def test_a_persons_resume_ages_out_the_human_escalation(session_factory):
    """Review of fbc54ddd6: the escalation.next == 'human' guard never aged
    out (only the expert's settle pops the record), so a goal once parked
    for a person was excluded from the daemon's resume for life.  A resume
    moves the human escalation to history; the next pause the daemon makes
    is the daemon's again."""
    from integrations.agent_engine.goal_manager import GoalManager
    db = session_factory()
    _goal(db, 'g-aged', {'pause_cause': 'needs_help', 'paused_by': 'daemon',
                         'pause_reason': 'Needs help: action 2', 'paused_at': T0.isoformat(),
                         'escalation': {'action_id': 2, 'next': 'human', 'told_at': 'x'}})
    goal = db.get(AgentGoal, 'g-aged')
    assert GoalManager.resume_goal(db, 'g-aged', resumed_by='person:owner')['success']
    db.commit()
    cfg = goal.config_json
    assert 'escalation' not in cfg and cfg['last_escalation']['next'] == 'human'
    ad._dispatch_backoff['g-aged'] = {'failures': 5, 'skip_until': 0}
    _auto_pause_for_dispatch_failures(db, goal, 'g-aged', 5, 'turn failed: 400')
    db.commit()
    assert _resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True) == ['g-aged']
