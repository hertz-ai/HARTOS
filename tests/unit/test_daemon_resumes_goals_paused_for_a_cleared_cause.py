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


def test_the_tick_wires_the_probe_both_successes_and_the_episode_reset():
    """Wiring in _tick, anchored on the code it sits next to: the probe runs
    before the active-goal query; both success sites (the plain dispatch and
    the parallel fan-out) release right after they pop their own backoff;
    the plain success resets the episode counter."""
    src = inspect.getsource(ad.AgentDaemon._tick)
    probe = src.index('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=False)')
    assert probe < src.index("AgentGoal.status == 'active'")
    assert src.count('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)') == 2
    pops = [i for i in range(len(src))
            if src.startswith('_dispatch_backoff.pop(goal_key, None)', i)]
    releases = [i for i in range(len(src))
                if src.startswith('_resume_goals_paused_for_a_cleared_cause(db, cause_cleared=True)', i)]
    for rel in releases:
        # right after its own pop, before the handoff that follows it
        assert any(0 < rel - p < 1500 for p in pops)
        assert rel < src.index('_settle_dispatched_goal(db, goal, goal_key', rel)
    assert src.index("_cfg.pop('daemon_resume_count', None)") < releases[-1]
    assert '_auto_pause_for_dispatch_failures(' in src
