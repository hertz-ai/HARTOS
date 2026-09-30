"""One identity, one vote: an owner and all their agents are ONE vote in the
FOR / AGAINST ratio, not one per agent.

Owner rulings: an agent counts as its owner, and no one entity monopolises
the hive (voting_rules, 2026-09-24).  tally_votes collapsed agents into
their owner for the QUORUM (distinct identities) but still summed weight per
vote row for the RATIO.  Measured by the batch review with this file's first
case at 058c01b05: five people, 2 FOR and 3 AGAINST, one of the FOR owning
ten agents that also vote FOR -> ratio 0.727 -> APPROVED.  Now each identity
contributes one vote: the human's own vote when they cast one, otherwise the
majority of that identity's agents (a tie is no vote).

And the vote route took voter_type from the request body.  An agent account
holds an api_token (UserService registers every agent with one), passes
require_auth, and could claim 'human': full weight, and past the "agents
cannot vote on security" rule.  voter_type is now derived from the account,
at the route and in the tally.

Real SQLite; the real service, the real route with the real require_auth.
Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social import models as social_models  # noqa: E402
from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)
from integrations.social.voting_rules import approval_verdict  # noqa: E402

DEFAULT = ('Cache warmup', 'Faster cache warmup lowers latency')
SECURITY = ('Tighten the security guardrail',
            'A stricter guardrail blocks the vulnerability')


@pytest.fixture
def factory(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'oneid.db'}")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(social_models, '_SessionLocal', f)
    yield f
    eng.dispose()


@pytest.fixture
def db(factory):
    s = factory()
    yield s
    s.close()


def _user(db, user_type='human', owner_id=None, api_token=None):
    u = User(username=f'livetest_oi_{uuid.uuid4().hex[:8]}',
             user_type=user_type, owner_id=owner_id, api_token=api_token)
    db.add(u)
    db.commit()
    return u


def _experiment(db, what=DEFAULT):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title=what[0],
        hypothesis=what[1], expected_outcome='o', status='voting')
    db.add(e)
    db.commit()
    return e


def _vote(db, exp_id, voter, value, voter_type=None, confidence=1.0):
    db.add(ExperimentVote(
        experiment_id=exp_id, voter_id=voter.id,
        voter_type=voter_type or voter.user_type, vote_value=value,
        confidence=confidence))
    db.commit()


def _tally(db, e):
    return ThoughtExperimentService.tally_votes(db, e.id)


def _ratio(t):
    d = t['total_for'] + t['total_against']
    return t['total_for'] / d if d else 0.0


def test_ten_agents_of_one_person_are_one_vote(db):
    """The review's case exactly."""
    e = _experiment(db)
    a = _user(db)
    _vote(db, e.id, a, 2)
    for _ in range(10):
        _vote(db, e.id, _user(db, 'agent', owner_id=a.id), 2)
    _vote(db, e.id, _user(db), 2)
    for _ in range(3):
        _vote(db, e.id, _user(db), -2)
    t = _tally(db, e)
    assert t['distinct_voters'] == 5 and t['distinct_supporters'] == 2
    assert round(_ratio(t), 4) == 0.4
    assert approval_verdict(t)['approved'] is False
    result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
    db.commit()
    assert result['success'] is False
    assert db.query(AgentGoal).count() == 0


def test_the_humans_own_vote_is_the_identitys_vote(db):
    """The owner voted AGAINST; three of their agents voted FOR.  One vote,
    AGAINST, and the owner is no supporter."""
    e = _experiment(db)
    owner = _user(db)
    _vote(db, e.id, owner, -1)
    for _ in range(3):
        _vote(db, e.id, _user(db, 'agent', owner_id=owner.id), 2)
    t = _tally(db, e)
    assert t['distinct_voters'] == 1 and t['distinct_supporters'] == 0
    assert t['total_for'] == 0 and t['total_against'] == 1.0


def test_without_the_human_the_agents_majority_is_the_vote(db):
    e = _experiment(db)
    owner = _user(db)
    for v in (2, 2, -2):
        _vote(db, e.id, _user(db, 'agent', owner_id=owner.id), v)
    t = _tally(db, e)
    assert t['distinct_voters'] == 1 and t['distinct_supporters'] == 1
    assert t['total_against'] == 0 and t['total_for'] > 0


def test_a_split_identity_casts_no_vote(db):
    """One FOR, one AGAINST: a tie in COUNT is no vote, however strong one
    side is (+2 against -1 would otherwise average to a FOR)."""
    e = _experiment(db)
    owner = _user(db)
    for v in (2, -1):
        _vote(db, e.id, _user(db, 'agent', owner_id=owner.id), v)
    t = _tally(db, e)
    assert t['distinct_voters'] == 0 and t['distinct_supporters'] == 0
    assert t['total_for'] == 0 and t['total_against'] == 0


def test_an_identity_that_only_abstained_is_no_voter(db):
    e = _experiment(db)
    owner = _user(db)
    _vote(db, e.id, owner, 0)
    _vote(db, e.id, _user(db, 'agent', owner_id=owner.id), 0)
    t = _tally(db, e)
    assert t['distinct_voters'] == 0
    assert t['total_for'] == 0 and t['total_against'] == 0


def test_separate_people_still_count_separately(db):
    """Control: the collapse is per identity, not per side."""
    e = _experiment(db)
    for v in (2, 2, -1):
        _vote(db, e.id, _user(db), v)
    t = _tally(db, e)
    assert t['distinct_voters'] == 3 and t['distinct_supporters'] == 2
    assert t['total_for'] == 2.0 and t['total_against'] == 1.0
    assert approval_verdict(t)['approved'] is True


def test_the_tally_weighs_an_agent_as_an_agent_whatever_its_row_says(db):
    """A row stored with voter_type 'human' by an agent account (the route
    used to trust the body) is weighed as the agent it is."""
    e = _experiment(db)   # technical_improvement: agent_weight 0.6
    agent = _user(db, 'agent', owner_id=_user(db).id)
    _vote(db, e.id, agent, 2, voter_type='human', confidence=0.5)
    t = _tally(db, e)
    assert t['agent_votes'] == 1 and t['human_votes'] == 0
    assert t['total_for'] == pytest.approx(0.3)


# ── the vote route derives voter_type from the account ─────────────────

@pytest.fixture
def client(factory):
    from flask import Flask
    from integrations.social.api_thought_experiments import (
        thought_experiments_bp)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(thought_experiments_bp)
    return app.test_client()


def _post(client, e, token, **body):
    return client.post(f'/api/social/experiments/{e.id}/vote',
                       json=dict({'vote_value': 2}, **body),
                       headers={'Authorization': f'Bearer {token}'})


def test_an_agent_account_cannot_vote_as_a_human(db, client):
    """Measured first: an agent account's api_token passes require_auth."""
    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id,
                  api_token='tok-agent-' + uuid.uuid4().hex)
    resp = _post(client, e, agent.api_token, voter_type='human',
                 confidence=1.0)
    assert resp.status_code == 200, resp.get_json()
    db.expire_all()
    row = db.query(ExperimentVote).filter_by(voter_id=agent.id).one()
    assert row.voter_type == 'agent'


def test_an_agent_account_cannot_vote_on_security_by_claiming_human(
        db, client):
    e = _experiment(db, SECURITY)
    agent = _user(db, 'agent', owner_id=_user(db).id,
                  api_token='tok-agent-' + uuid.uuid4().hex)
    resp = _post(client, e, agent.api_token, voter_type='human')
    assert resp.status_code == 400
    db.expire_all()
    assert db.query(ExperimentVote).filter_by(voter_id=agent.id).count() == 0


def test_a_person_is_a_human_whatever_the_body_says(db, client):
    e = _experiment(db)
    person = _user(db, api_token='tok-person-' + uuid.uuid4().hex)
    resp = _post(client, e, person.api_token, voter_type='agent',
                 confidence=0.1)
    assert resp.status_code == 200
    db.expire_all()
    row = db.query(ExperimentVote).filter_by(voter_id=person.id).one()
    assert row.voter_type == 'human' and row.confidence == 1.0
