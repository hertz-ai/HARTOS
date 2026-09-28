"""The agent vote tool casts the CALLING agent's vote, counted as its owner;
never a person's, never another agent's.

cast_experiment_vote is the agent entry.  It took voter_id and voter_type as
given, so an agent could name any person's id with voter_type 'human' and
cast that person's full-weight vote: one caller could manufacture the
distinct-identity quorum (>= 3 identities, >= 2 FOR) the owner ruled must
come from different people.  d89d50223 limited the argument to an agent,
but the argument still chose WHICH agent, so an agent could vote as another
owner's agent.

Now the voter is the CALLER, never the argument: the agent whose turn is
running, from the request's thread-local prompt_id (hartos.threadlocal,
set by the /chat handler, the same context the VLM loop and the shell tool
read), resolved to its users row (user_type 'agent', by id or prompt id).
The vote is recorded under that row as an agent vote; tally_votes counts it
as its owner's identity.  voter_id may only name that same agent (or be
empty); naming anyone else is refused and nothing is written.  No caller,
no vote.

Measured 2026-09-27 (scratchpad/tl_probe.py): no path registers this tool on
an autogen agent; its one runtime caller is the MCP bridge.  There the
thread-local is either empty (fresh thread) or LEFT OVER from the last
/chat on that reused worker thread (prompt_id '8865956', user_id of that
chat).  So the bridge runs every tool with the caller context cleared, and
an MCP vote is refused: an MCP call is no agent's turn.

Real SQLite; the tool runs through the real db_session and the real MCP
bridge.
"""
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from hartos.threadlocal import thread_local_data  # noqa: E402
from integrations.social import models as social_models  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'agentvote.db'}")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(social_models, '_SessionLocal', f)
    s = f()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture(autouse=True)
def caller():
    """The running agent turn, as /chat sets it; restored afterwards."""
    before = (thread_local_data.get_prompt_id(), thread_local_data.get_user_id())

    def _as(agent, user_id=None):
        thread_local_data.set_prompt_id(agent.agent_id if agent else None)
        thread_local_data.set_user_id(user_id)
    _as(None)
    yield _as
    thread_local_data.set_prompt_id(before[0])
    thread_local_data.set_user_id(before[1])


def _user(db, user_type='human', owner_id=None):
    u = User(username=f'livetest_av_{uuid.uuid4().hex[:8]}',
             user_type=user_type, owner_id=owner_id,
             agent_id=(str(uuid.uuid4().int)[:11]
                       if user_type == 'agent' else None))
    db.add(u)
    db.commit()
    return u


def _experiment(db):
    # technical_improvement: agents may vote here.
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title='Cache warmup',
        hypothesis='Faster cache warmup lowers latency', expected_outcome='o',
        status='voting')
    db.add(e)
    db.commit()
    return e


def _cast(e, voter_id='', voter_type='agent', value=2):
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)
    return json.loads(cast_experiment_vote(e.id, voter_id, vote_value=value,
                                           voter_type=voter_type,
                                           confidence=1.0))


def _rows(db, e):
    db.expire_all()
    return [(v.voter_id, v.voter_type)
            for v in db.query(ExperimentVote).filter_by(experiment_id=e.id)]


@pytest.mark.parametrize('voter_type', ['human', 'agent'])
def test_naming_a_person_is_refused_and_writes_nothing(db, caller, voter_type):
    e = _experiment(db)
    caller(_user(db, 'agent', owner_id=_user(db).id))
    out = _cast(e, _user(db).id, voter_type=voter_type)
    assert out['success'] is False
    assert _rows(db, e) == []


def test_naming_another_agent_is_refused(db, caller):
    e = _experiment(db)
    me = _user(db, 'agent', owner_id=_user(db).id)
    other = _user(db, 'agent', owner_id=_user(db).id)
    caller(me)
    for name in (other.id, other.agent_id):
        assert _cast(e, name)['success'] is False
    assert _rows(db, e) == []


@pytest.mark.parametrize('name', ['steward', 'no-such-id'])
def test_a_name_that_is_not_the_caller_is_refused(db, caller, name):
    e = _experiment(db)
    caller(_user(db, 'agent', owner_id=_user(db).id))
    assert _cast(e, name)['success'] is False
    assert _rows(db, e) == []


@pytest.mark.parametrize('how', ['empty', 'row_id', 'prompt_id'])
def test_the_caller_votes_as_itself_counted_as_its_owner(db, caller, how):
    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id)
    caller(agent)
    name = {'empty': '', 'row_id': agent.id, 'prompt_id': agent.agent_id}[how]
    out = _cast(e, name, voter_type='human')     # the claim is ignored
    assert out['success'] is True, out
    assert _rows(db, e) == [(agent.id, 'agent')]
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['agent_votes'] == 1 and tally['human_votes'] == 0
    assert tally['distinct_voters'] == 1


def test_no_caller_no_vote_even_naming_a_real_agent(db, caller):
    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id)
    caller(None)
    assert _cast(e, agent.id)['success'] is False
    assert _rows(db, e) == []


def test_a_caller_that_is_a_person_is_no_agent(db):
    """The thread's prompt id resolves only to an agent row."""
    e = _experiment(db)
    person = _user(db)
    thread_local_data.set_prompt_id(person.id)
    assert _cast(e, person.id)['success'] is False
    assert _rows(db, e) == []


def test_two_agents_of_one_owner_are_one_identity(db, caller):
    e = _experiment(db)
    owner = _user(db)
    a1 = _user(db, 'agent', owner_id=owner.id)
    a2 = _user(db, 'agent', owner_id=owner.id)
    caller(a1)
    assert _cast(e)['success'] is True
    caller(a2)
    assert _cast(e)['success'] is True
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['agent_votes'] == 2
    assert tally['distinct_voters'] == 1
    assert tally['distinct_supporters'] == 1


def test_one_caller_cannot_build_a_quorum_of_people(db, caller):
    """The reviewer's attack (stw_probe2.py): three people's ids."""
    e = _experiment(db)
    caller(_user(db, 'agent', owner_id=_user(db).id))
    for _ in range(3):
        _cast(e, _user(db).id, voter_type='human')
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['distinct_voters'] == 0 and tally['quorum_met'] is False


def test_over_mcp_a_left_over_chat_caller_casts_nothing(db, caller,
                                                        monkeypatch):
    """The real MCP bridge, on a thread a /chat left its agent on: the vote
    is refused, and the thread's context is intact after the call."""
    from integrations.mcp import mcp_http_bridge as bridge

    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id)
    caller(agent, user_id='owner-of-last-chat')
    payload, status = bridge._invoke_tool(
        'cast_experiment_vote',
        {'experiment_id': e.id, 'voter_id': agent.id, 'vote_value': 2})
    assert status == 200
    assert payload['result']['success'] is False
    assert _rows(db, e) == []
    assert thread_local_data.get_prompt_id() == agent.agent_id
    assert thread_local_data.get_user_id() == 'owner-of-last-chat'
