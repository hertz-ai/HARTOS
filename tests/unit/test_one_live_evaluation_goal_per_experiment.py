"""An experiment has at most ONE live evaluation goal.

MEASURED LIVE 2026-09-24 on the installed build (HARTOS 78c68934a): the three
LiveProbe experiments were dispatched by auto-evolve at 2026-09-23 20:09Z and
again at 02:24Z -- six goals for three experiments, all `paused`. The cause is
structural: a paused goal is not terminal, so the cycle stays `running` until
AUTO_EVOLVE_SESSION_MAX_AGE_S (6 h) ages it out; the experiment is still
`evaluating` with no evaluation recorded, so the next cycle gathers it again
and request_agent_evaluation made ANOTHER goal. Three more every ~6 h, and the
live ones never finish.

request_agent_evaluation is the single creator (auto-evolve and the REST
route both call it), so the rule lives there: while a live goal exists for the
experiment, it is returned instead of a new one. Once that goal is terminal
(completed / failed / archived -- the same set auto_evolve.reconcile treats
as terminal) a retry creates a fresh goal, as before.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture
def experiment(db):
    u = User(username=f'u_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(u)
    db.flush()
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=u.id, title='LiveProbe-like',
        hypothesis='h', expected_outcome='o', status='evaluating')
    db.add(e)
    db.commit()
    return e


def _goals_for(db, exp_id):
    out = []
    for g in db.query(AgentGoal).all():
        cfg = g.config_json or {}
        if isinstance(cfg, str):
            import json
            cfg = json.loads(cfg or '{}')
        if cfg.get('experiment_id') == exp_id:
            out.append(g)
    return out


def test_a_second_request_reuses_the_live_goal(db, experiment):
    first = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    db.commit()
    assert first['success'] and first['goal_id']
    second = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    db.commit()
    assert second['success'] and second['goal_id'] == first['goal_id']
    assert len(_goals_for(db, experiment.id)) == 1, (
        'a second evaluation goal was created while the first was still live')


@pytest.mark.parametrize('live_status', ['active', 'paused'])
def test_a_paused_or_active_goal_counts_as_live(db, experiment, live_status):
    first = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    g = db.query(AgentGoal).filter_by(id=first['goal_id']).first()
    g.status = live_status
    db.commit()
    ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    db.commit()
    assert len(_goals_for(db, experiment.id)) == 1


@pytest.mark.parametrize('ended', ['completed', 'failed', 'archived'])
def test_a_retry_after_the_goal_ended_creates_a_fresh_one(db, experiment, ended):
    first = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    g = db.query(AgentGoal).filter_by(id=first['goal_id']).first()
    g.status = ended
    db.commit()
    again = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    db.commit()
    assert again['success'] and again['goal_id'] != first['goal_id']
    assert len(_goals_for(db, experiment.id)) == 2


def test_other_experiments_are_not_blocked(db, experiment):
    other = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=experiment.creator_id, title='other',
        hypothesis='h', expected_outcome='o', status='evaluating')
    db.add(other)
    db.commit()
    a = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    b = ThoughtExperimentService.request_agent_evaluation(db, other.id)
    db.commit()
    assert a['goal_id'] != b['goal_id']


def test_a_goal_whose_config_is_stored_as_text_is_still_found(db, experiment):
    """Some backends hand config_json back as a JSON string, not a dict.
    The lookup must parse it rather than miss the live goal (and must not
    raise: `json` has to be importable in the service module)."""
    import json as _json
    first = ThoughtExperimentService.request_agent_evaluation(db, experiment.id)
    db.commit()
    real_query = db.query

    class _TextConfig:
        def __init__(self, g):
            self._g = g

        def __getattr__(self, name):
            if name == 'config_json':
                return _json.dumps(self._g.config_json)
            return getattr(self._g, name)

    class _Q:
        def __init__(self, q):
            self._q = q

        def filter(self, *a, **k):
            return _Q(self._q.filter(*a, **k))

        def all(self):
            return [_TextConfig(g) for g in self._q.all()]

    def query(model, *rest):
        q = real_query(model, *rest)
        return _Q(q) if model is AgentGoal else q

    db.query = query
    try:
        found = ThoughtExperimentService._live_evaluation_goal(db, experiment.id)
    finally:
        db.query = real_query
    assert found is not None and found.id == first['goal_id']
