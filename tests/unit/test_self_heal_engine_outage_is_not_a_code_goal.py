"""An LLM engine failing a generation is not a code bug, so it makes no coding goal.

integrations/agent_lightning/wrapper.py reports to the self-heal pipeline when
the SERVING ENGINE failed a generation (5xx, or a dropped/timed-out
connection) after its re-samples: report_subsystem_failure('llm', <agent>,
exc, 'generate_reply').  SelfHealingDispatcher turned every such pattern into
a self_heal goal, "Fix InternalServerError in llm.<agent name>.generate_reply",
which goal_manager hands to the coding agent.  The engine's availability is
the LLM watchdog's (integrations/service_tools/model_lifecycle.py,
[LLM-WATCHDOG]); no source edit restarts llama-server.

Measured 2026-10-05 on the owner's desktop (live DB, read-only): 120 of the
214 self_heal goals ever created carry this signature (module llm.*, function
generate_reply), 12 of them active; the reporter names the agent, so one
outage made one goal per agent that hit it, and the coding agent spent its
turns searching for a source file named after an agent.

    python -m pytest tests/unit/test_self_heal_engine_outage_is_not_a_code_goal.py -q
"""
import os
import sys

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


def _engine_500():
    import httpx
    import openai
    request = httpx.Request('POST', 'http://127.0.0.1:8080/v1/chat/completions')
    return openai.InternalServerError(
        'Internal Server Error', response=httpx.Response(500, request=request),
        body=None)


def _engine_unreachable():
    import httpx
    import openai
    return openai.APIConnectionError(
        request=httpx.Request('POST', 'http://127.0.0.1:8080/v1/chat/completions'))


def _report_engine_failure(agent, exc, times=3):
    """What agent_lightning's wrapper reports after its re-samples fail."""
    from hartos.exception_collector import report_subsystem_failure
    for _ in range(times):
        report_subsystem_failure(subsystem='llm', identifier=agent, exc=exc,
                                 function='generate_reply',
                                 failure_kind='generation_5xx',
                                 retries_exhausted=2)


def _self_heal_goals(db):
    return db.query(AgentGoal).filter(AgentGoal.goal_type == 'self_heal').all()


def test_an_engine_outage_makes_no_coding_goal(db, collector, dispatcher):
    for agent in ('reuse_recipe_assistant_w80_1_1', 'create_recipe_assistant_2_2'):
        _report_engine_failure(agent, _engine_500())
    _report_engine_failure('reuse_recipe_assistant_3_3', _engine_unreachable())
    created = dispatcher.check_and_dispatch(db)
    assert created == 0
    assert _self_heal_goals(db) == []
    # handled: the patterns are not re-read every five minutes
    import time
    assert collector.get_patterns(since=time.time() - 3600, min_count=1) == {}


def test_a_code_failure_still_makes_its_goal(db, collector, dispatcher):
    from hartos.exception_collector import report_subsystem_failure
    for _ in range(3):
        report_subsystem_failure('tts', 'probe', RuntimeError('probe died'), 'probe')
    _report_engine_failure('reuse_recipe_assistant_w80_1_1', _engine_500())
    assert dispatcher.check_and_dispatch(db) == 1
    (goal,) = _self_heal_goals(db)
    assert goal.title == 'Fix RuntimeError in tts.probe.probe'


def _goal(db, status, title, module, function, exc_type):
    goal = AgentGoal(goal_type='self_heal', title=title, status=status,
                     config_json={'mode': 'self_heal', 'source_module': module,
                                  'source_function': function,
                                  'exc_type': exc_type,
                                  'pattern_key': f'{exc_type}::{module}::{function}'})
    db.add(goal)
    db.flush()
    return goal


def test_existing_engine_outage_goals_are_archived_and_code_goals_are_kept(
        db, collector, dispatcher):
    active = _goal(db, 'active', 'Fix InternalServerError in llm.reuse_recipe_assistant_w80_1_1.generate_reply',
                   'llm.reuse_recipe_assistant_w80_1_1', 'generate_reply', 'InternalServerError')
    paused = _goal(db, 'paused', 'Fix APIConnectionError in llm.create_recipe_assistant_2_2.generate_reply',
                   'llm.create_recipe_assistant_2_2', 'generate_reply', 'APIConnectionError')
    tts = _goal(db, 'active', 'Self-heal: tts.probe (RuntimeError)',
                'tts.probe', 'probe', 'RuntimeError')
    dispatcher.check_and_dispatch(db)
    assert active.status == 'archived'
    assert paused.status == 'archived'
    assert 'LLM watchdog' in (active.config_json or {}).get('archived_reason', '')
    assert tts.status == 'active'


def test_a_failing_archive_sweep_does_not_stop_a_new_goal(
        db, collector, dispatcher, monkeypatch, caplog):
    """Review of bef0e1e44 (08:00Z): the sweep ran first and unguarded.  A
    goal writer that raised made the whole check raise, so no new goal was
    made, and both callers (agent_daemon's tick, exception_watcher) log that
    only at DEBUG.  The sweep is housekeeping; making goals is the check."""
    import logging
    from hartos.exception_collector import report_subsystem_failure
    from integrations.agent_engine.goal_manager import GoalManager
    _goal(db, 'active', 'Fix InternalServerError in llm.reuse_x.generate_reply',
          'llm.reuse_x', 'generate_reply', 'InternalServerError')

    def writer_down(*_a, **_k):
        raise RuntimeError('goal writer down')

    monkeypatch.setattr(GoalManager, 'update_goal', writer_down)
    for _ in range(3):
        report_subsystem_failure('tts', 'probe', RuntimeError('probe died'), 'probe')
    with caplog.at_level(logging.WARNING, logger='hevolve_social'):
        assert dispatcher.check_and_dispatch(db) == 1
    assert 'Fix RuntimeError in tts.probe.probe' in [g.title for g in _self_heal_goals(db)]
    assert any(r.levelno == logging.WARNING and 'archive sweep failed' in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_raising_check_leaves_the_watchers_threshold_as_it_was(
        db, collector, dispatcher, monkeypatch):
    """exception_watcher lowers the dispatcher's threshold to 1 for a critical
    exception and put it back only when the check returned: one raise left
    every later check making a goal from a single occurrence."""
    from hartos.exception_collector import report_subsystem_failure
    from integrations.agent_engine.exception_watcher import ExceptionWatcher
    ExceptionWatcher.reset_instance()
    watcher = ExceptionWatcher.get_instance()
    watcher.assign_watcher('u1', 'watcher-agent')
    try:
        report_subsystem_failure('tts', 'probe', MemoryError('out of memory'), 'probe')

        def check_fails(_db):
            raise RuntimeError('check failed')

        monkeypatch.setattr(dispatcher, 'check_and_dispatch', check_fails)
        watcher.process_exceptions(db)
        assert dispatcher._min_occurrences == 3
    finally:
        ExceptionWatcher.reset_instance()
