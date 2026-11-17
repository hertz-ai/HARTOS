"""One recurring error gets one self-heal goal, even after that goal is paused.

Two writers file self_heal goals for a recurring failure, and each skips a
failure that a goal already targets:
  - SelfHealingDispatcher, for an exception pattern seen 3+ times in an hour
    (_is_already_being_fixed, keyed on config['pattern_key']);
  - core.error_advice, for a failure in code wrapped with
    agent_remediation=True (_try_agent_remediation, keyed on
    config['fingerprint']).
Both checks looked at ACTIVE goals only.  The daemon pauses a self_heal goal
that keeps failing or makes no progress, so the next occurrence of the same
failure found no active goal and filed a new one, which was paused in turn.

Measured 2026-11-04 on the owner's desktop (hevolve_database.db, read-only):
  - SelfHealingDispatcher: 331 goals over 124 patterns; 205 of its 207 repeat
    goals were filed while an earlier goal for the same pattern was paused.
    181 of them are the one pattern
    ReadTimeout::world_model_bridge::ingest_sensor_batch, filed from 09-20 to
    11-04, all 181 paused, none completed.
  - error_advice: since its own dedup landed (e807cdc46, 05-31), 22 repeat
    goals, all 22 filed while an earlier goal for the same fingerprint was
    paused (the latest on 11-03).

    python -m pytest tests/unit/test_self_heal_one_goal_per_error.py -q
"""
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import AgentGoal, Base

# Same deterministic-schema guard test_agent_engine.py uses: tenant_filter may
# add tenant_id to the shared metadata at runtime.
if 'tenant_id' not in AgentGoal.__table__.c:
    AgentGoal.__table__.append_column(
        Column('tenant_id', String(64), nullable=True, index=True))

PATTERN = 'ReadTimeout::world_model_bridge::ingest_sensor_batch'


class ReadTimeout(Exception):
    """Named like the requests exception the live pattern carries."""


@pytest.fixture
def db():
    engine = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def collector():
    from hartos.exception_collector import ExceptionCollector
    ExceptionCollector.reset_instance()
    yield ExceptionCollector.get_instance()
    ExceptionCollector.reset_instance()


@pytest.fixture
def dispatcher():
    from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher
    SelfHealingDispatcher.reset_instance()
    yield SelfHealingDispatcher.get_instance()
    SelfHealingDispatcher.reset_instance()


# --- SelfHealingDispatcher: keyed on the exception pattern ---

def _the_error_recurs(times=3):
    from hartos.exception_collector import record_exception
    for _ in range(times):
        record_exception(ReadTimeout('Read timed out. (read timeout=10)'),
                         module='world_model_bridge', function='ingest_sensor_batch')


def _check(dispatcher, db):
    dispatcher._last_check = 0          # the check runs at most every 5 minutes
    return dispatcher.check_and_dispatch(db)


def _goals_for_the_error(db):
    return [g for g in db.query(AgentGoal).filter(AgentGoal.goal_type == 'self_heal').all()
            if (g.config_json or {}).get('pattern_key') == PATTERN]


def test_a_paused_goal_for_the_error_gets_no_second_goal(db, collector, dispatcher):
    _the_error_recurs()
    assert _check(dispatcher, db) == 1
    (goal,) = _goals_for_the_error(db)

    goal.status = 'paused'              # what the daemon does to a goal that keeps failing
    db.flush()

    _the_error_recurs()
    assert _check(dispatcher, db) == 0
    assert _goals_for_the_error(db) == [goal]


def test_an_active_goal_for_the_error_still_gets_no_second_goal(db, collector, dispatcher):
    _the_error_recurs()
    assert _check(dispatcher, db) == 1
    _the_error_recurs()
    assert _check(dispatcher, db) == 0
    assert len(_goals_for_the_error(db)) == 1


@pytest.mark.parametrize('closed', ['completed', 'archived'])
def test_a_closed_goal_lets_a_recurring_error_get_a_new_goal(db, collector, dispatcher, closed):
    # A fix that was finished (or archived) and the error came back: that is
    # new work, so a new goal is still filed.
    _the_error_recurs()
    assert _check(dispatcher, db) == 1
    (goal,) = _goals_for_the_error(db)
    goal.status = closed
    db.flush()

    _the_error_recurs()
    assert _check(dispatcher, db) == 1
    assert len(_goals_for_the_error(db)) == 2


# --- core.error_advice: the same check, keyed on the failure's fingerprint ---

def _error_advice_sees_the_failure(db, monkeypatch):
    """One failure through error_advice's remediation path, on the test DB."""
    import core.error_advice as error_advice
    import integrations.social.models as models

    @contextmanager
    def _session():
        yield db
        db.flush()

    monkeypatch.setattr(models, 'db_session', _session)
    error_advice._try_agent_remediation(
        'tts.probe', RuntimeError('tts probe failed'), {'subsystem': 'tts'}, 'high')


def _error_advice_goals(db):
    return db.query(AgentGoal).filter(AgentGoal.goal_type == 'self_heal',
                                      AgentGoal.created_by == 'error_advice').all()


def test_error_advice_a_paused_goal_for_the_failure_gets_no_second_goal(db, monkeypatch):
    _error_advice_sees_the_failure(db, monkeypatch)
    (goal,) = _error_advice_goals(db)

    goal.status = 'paused'
    db.flush()

    _error_advice_sees_the_failure(db, monkeypatch)
    assert _error_advice_goals(db) == [goal]


def test_error_advice_an_active_goal_for_the_failure_still_gets_no_second_goal(db, monkeypatch):
    _error_advice_sees_the_failure(db, monkeypatch)
    _error_advice_sees_the_failure(db, monkeypatch)
    assert len(_error_advice_goals(db)) == 1


@pytest.mark.parametrize('closed', ['completed', 'archived'])
def test_error_advice_a_closed_goal_lets_the_failure_get_a_new_goal(db, monkeypatch, closed):
    _error_advice_sees_the_failure(db, monkeypatch)
    (goal,) = _error_advice_goals(db)
    goal.status = closed
    db.flush()

    _error_advice_sees_the_failure(db, monkeypatch)
    assert len(_error_advice_goals(db)) == 2
