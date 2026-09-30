"""A vote is the steward's only when a signed-in human who holds the
steward role cast it.

058c01b05 made the steward's FOR vote part of the one approval rule, but it
recognised the steward by voter_id == 'steward': a string.  The agent tool
cast_experiment_vote passes any voter_id it is given, so any agent could
vote as the steward and approve a security_guardrail experiment (measured at
HEAD with this file: the goal started).

The steward is the account that holds the central role, the one
auth.require_central admits (User.role == 'central', or the is_admin flag
UserService.set_user_role keeps in step with it); no new role store.  Now:
  - only a registered HUMAN account holding that role is the steward; an
    agent never is, whoever owns it and whatever its row says (an agent
    counts as its owner for the quorum, never as the steward);
  - the literal voter_id 'steward' carries no weight of its own;
  - the agent tool cannot vote as the steward: it is not a signed-in
    human, so it refuses a voter_id that resolves to the steward.  The
    steward votes through the signed-in route (voter_id from the JWT).
  - decide()'s steward gate reads the same tally, so there is one steward
    check.

Real SQLite; the agent tool runs through the real db_session.
Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import json
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

SECURITY = ('Tighten the security guardrail',
            'A stricter guardrail blocks the vulnerability')


@pytest.fixture
def factory(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'steward.db'}")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    # db_session (the agent tool's session) uses this factory.
    monkeypatch.setattr(social_models, '_SessionLocal', f)
    yield f
    eng.dispose()


@pytest.fixture
def db(factory):
    s = factory()
    yield s
    s.close()


def _user(db, user_type='human', role='flat', owner_id=None, is_admin=False):
    u = User(username=f'livetest_stw_{uuid.uuid4().hex[:8]}',
             user_type=user_type, role=role, owner_id=owner_id,
             is_admin=is_admin)
    db.add(u)
    db.commit()
    return u


def _experiment(db):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title=SECURITY[0],
        hypothesis=SECURITY[1], expected_outcome='o', status='voting')
    db.add(e)
    db.commit()
    return e


def _vote(db, exp_id, voter_id, value, voter_type='human'):
    db.add(ExperimentVote(experiment_id=exp_id, voter_id=voter_id,
                          voter_type=voter_type, vote_value=value,
                          confidence=1.0))
    db.commit()


def _people_approve(db, exp_id):
    """Quorum and 0.8 met by people alone: 5 FOR / 1 AGAINST."""
    for v in (2, 2, 2, 2, 2, -1):
        _vote(db, exp_id, _user(db).id, v)


def _evaluate(db, e):
    result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
    db.commit()
    return result, db.query(AgentGoal).count()


def test_an_agent_voting_as_steward_through_the_tool_is_not_the_steward(db):
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)

    e = _experiment(db)
    _people_approve(db, e.id)
    cast_experiment_vote(e.id, 'steward', vote_value=2,
                         voter_type='human', confidence=1.0)
    db.expire_all()

    assert ThoughtExperimentService.tally_votes(db, e.id)['steward_vote'] is None
    result, goals = _evaluate(db, e)
    assert result['success'] is False
    assert result['verdict']['reason'] == 'steward_required'
    assert goals == 0


def test_a_steward_role_human_satisfies_steward_required(db):
    e = _experiment(db)
    _people_approve(db, e.id)
    steward = _user(db, role='central', is_admin=True)
    _vote(db, e.id, steward.id, 2)

    result, goals = _evaluate(db, e)
    assert result['success'] is True and result['goal_id']
    assert goals == 1


def test_a_steward_against_blocks(db):
    e = _experiment(db)
    _people_approve(db, e.id)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, -2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False and goals == 0


def test_one_steward_against_is_not_outvoted_by_another_steward_for(db):
    e = _experiment(db)
    _people_approve(db, e.id)
    for _ in range(3):   # 9 FOR / 2 AGAINST overall = 0.82: over 0.8
        _vote(db, e.id, _user(db).id, 2)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, 2)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, -1)
    result, goals = _evaluate(db, e)
    assert result['success'] is False and goals == 0
    assert result['verdict']['reason'] == 'steward_required'


def test_a_steward_against_cast_first_is_not_undone_by_a_later_for(db):
    """Order must not matter: the AGAINST first, then another steward's FOR.
    A last-vote-wins tally passed the FOR-then-AGAINST case above."""
    e = _experiment(db)
    _people_approve(db, e.id)
    for _ in range(3):
        _vote(db, e.id, _user(db).id, 2)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, -1)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, 2)
    result, goals = _evaluate(db, e)
    assert result['success'] is False and goals == 0
    assert result['verdict']['reason'] == 'steward_required'


def test_a_steward_abstain_is_no_steward_answer_anywhere(db):
    """Abstain (0) is not the steward's answer: approval stays blocked and
    decide() still waits for the steward, from the same rule."""
    e = _experiment(db)
    _people_approve(db, e.id)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, 0)
    result, goals = _evaluate(db, e)
    assert result['verdict']['reason'] == 'steward_required' and goals == 0
    out = ThoughtExperimentService.decide(db, e.id, 'go')
    assert out.get('error') == 'steward_vote_required'


def test_a_steward_against_still_lets_the_experiment_be_decided(db):
    """decide() records a decision either way; the steward answering AGAINST
    is an answer, so it no longer waits."""
    e = _experiment(db)
    _vote(db, e.id, _user(db, role='central', is_admin=True).id, -2)
    out = ThoughtExperimentService.decide(db, e.id, 'rejected')
    assert out.get('status') == 'decided'


@pytest.mark.parametrize('role, is_admin, status', [
    ('central', False, 200),
    ('flat', True, 200),
    ('regional', False, 403),
    ('flat', False, 403),
])
def test_require_admin_asks_the_one_central_check(factory, monkeypatch,
                                                  role, is_admin, status):
    """require_admin and require_central admit exactly the accounts
    auth.holds_central_role does."""
    from flask import Flask, jsonify
    from integrations.social import auth

    s = factory()
    u = _user(s, role=role, is_admin=is_admin)
    monkeypatch.setattr(auth, '_get_user_from_token',
                        lambda token: (u, factory()))
    app = Flask(__name__)

    @app.route('/a')
    @auth.require_admin
    def _a():
        return jsonify({'ok': True})

    @app.route('/c')
    @auth.require_central
    def _c():
        return jsonify({'ok': True})

    client = app.test_client()
    h = {'Authorization': 'Bearer t'}
    assert client.get('/a', headers=h).status_code == status
    assert client.get('/c', headers=h).status_code == status
    s.close()


def test_an_agent_the_steward_owns_is_not_the_steward(db):
    """It counts as its owner for the quorum, never as the steward."""
    e = _experiment(db)
    _people_approve(db, e.id)
    steward = _user(db, role='central', is_admin=True)
    agent = _user(db, user_type='agent', owner_id=steward.id)
    _vote(db, e.id, agent.id, 2, voter_type='agent')

    result, goals = _evaluate(db, e)
    assert result['verdict']['reason'] == 'steward_required'
    assert goals == 0


def test_an_agent_row_carrying_the_role_is_not_the_steward(db):
    e = _experiment(db)
    _people_approve(db, e.id)
    agent = _user(db, user_type='agent', role='central', is_admin=True)
    _vote(db, e.id, agent.id, 2)

    result, goals = _evaluate(db, e)
    assert result['verdict']['reason'] == 'steward_required'
    assert goals == 0


def test_an_ordinary_human_is_not_the_steward(db):
    e = _experiment(db)
    _people_approve(db, e.id)
    _vote(db, e.id, _user(db, role='regional').id, 2)
    result, goals = _evaluate(db, e)
    assert result['verdict']['reason'] == 'steward_required'
    assert goals == 0


def test_the_tool_refuses_to_vote_as_the_real_steward(db):
    """The tool is no signed-in human: naming the steward's own id must not
    let an agent cast the steward's vote."""
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)

    e = _experiment(db)
    steward = _user(db, role='central', is_admin=True)
    out = json.loads(cast_experiment_vote(e.id, steward.id, vote_value=2,
                                          voter_type='human'))
    db.expire_all()

    assert out['success'] is False
    assert db.query(ExperimentVote).filter_by(voter_id=steward.id).count() == 0


def test_the_tool_casts_no_persons_vote_steward_or_not(db):
    """The tool casts agent votes only (test_agent_tool_votes_as_the_agent):
    a person's id is refused like the steward's, and nothing is written.
    Its control, an agent's vote that is written, is the next test."""
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)

    e = _experiment(db)
    person = _user(db)
    out = json.loads(cast_experiment_vote(e.id, person.id, vote_value=1,
                                          voter_type='human'))
    db.expire_all()
    assert out['success'] is False, out
    assert db.query(ExperimentVote).filter_by(voter_id=person.id).count() == 0


def test_an_agents_tool_vote_counts_as_its_owner_never_as_the_steward(db):
    """Through the real agent tool: an agent the steward owns votes, and it
    counts as its owner for the quorum (one identity with the owner), but
    its vote is never the steward's.  Only the steward's own vote is."""
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)

    # technical_improvement: agents may vote here (security takes none).
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title='Cache warmup',
        hypothesis='Faster cache warmup lowers latency', expected_outcome='o',
        status='voting')
    db.add(e)
    db.commit()
    steward = _user(db, role='central', is_admin=True)
    # Even an agent row that carries its owner's central role.
    agent = _user(db, user_type='agent', owner_id=steward.id,
                  role='central', is_admin=True)
    agent.agent_id = '77001'
    db.commit()
    # The tool votes for the agent whose turn is running (thread-local
    # prompt id, test_agent_tool_votes_as_the_agent).
    from hartos.threadlocal import thread_local_data
    before = thread_local_data.get_prompt_id()
    thread_local_data.set_prompt_id('77001')
    try:
        _agent_turn_votes(db, e, agent, steward, cast_experiment_vote)
    finally:
        thread_local_data.set_prompt_id(before)


def _agent_turn_votes(db, e, agent, steward, cast_experiment_vote):
    out = json.loads(cast_experiment_vote(e.id, agent.id, vote_value=2,
                                          voter_type='agent', confidence=1.0))
    db.expire_all()
    assert out['success'] is True, out
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['agent_votes'] == 1
    assert tally['distinct_voters'] == 1          # the owner
    assert tally['steward_vote'] is None, 'the agent voted as the steward'

    # Naming its owner from the tool does not make the agent the steward.
    out = json.loads(cast_experiment_vote(e.id, steward.id, vote_value=2,
                                          voter_type='human'))
    db.expire_all()
    assert out['success'] is False
    assert ThoughtExperimentService.tally_votes(db, e.id)['steward_vote'] is None

    # The owner voting as themself (the signed-in route) is still one
    # identity with their agent, and is the steward.
    _vote(db, e.id, steward.id, 2)
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['distinct_voters'] == 1
    assert tally['steward_vote'] == 2


def test_decide_asks_the_same_steward_rule(db):
    e = _experiment(db)
    _vote(db, e.id, 'steward', 2)
    out = ThoughtExperimentService.decide(db, e.id, 'go')
    assert out.get('error') == 'steward_vote_required'

    _vote(db, e.id, _user(db, role='central', is_admin=True).id, 2)
    out = ThoughtExperimentService.decide(db, e.id, 'go')
    assert out.get('status') == 'decided'
