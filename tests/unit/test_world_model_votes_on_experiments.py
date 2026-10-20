"""The world model votes on thought experiments.

Owner decision, 2026-10-19: agents AND the world model may vote on thought
experiments; the quorum and the 2/3 rule stay as they are.  Measured on the
owner's node (hevolveai Master 11.462 G/H): 811 experiments, and in the
table's whole life no real experiment had received a single vote, so
auto-evolve could select nothing but test fixtures.

hevolveai's half (C329-C329g, reviewed) is POST /v1/world_model/score: how
surprised the node's own predictor is that a hypothesis leads to its
expected outcome, measured without learning either.  vote +1 (the claim
surprises the node more than its typical error: running it would teach
it), -1 (it does not), 0 (abstain); HTTP 503 while a learn holds the
learner (retry later).  The review that cleared HARTOS to call it set the
conditions pinned here: always send the experiment's OWN time as UTC epoch
seconds, and treat 503 or an abstention as "retry later".

Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.agent_engine.world_model_bridge import (  # noqa: E402
    WorldModelBridge)
from integrations.social.models import (  # noqa: E402
    Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    WORLD_MODEL_VOTER, ThoughtExperimentService)

CREATED = dt.datetime(2026, 10, 20, 6, 0, 0)  # naive UTC, as func.now() stores it


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    yield s
    s.close()


def _experiment(db, status='voting', expected='p50 latency falls by a third',
                created=CREATED, hypothesis=None):
    creator = User(username=f'wmv_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(creator)
    db.flush()
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=creator.id, title='Cache warmup',
        hypothesis=hypothesis or f'Faster cache warmup lowers latency {uuid.uuid4().hex[:4]}',
        expected_outcome=expected, status=status, created_at=created)
    db.add(e)
    db.commit()
    return e


class _Bridge:
    """Stands in for WorldModelBridge.score_claim; records every call."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def score_claim(self, **kw):
        self.calls.append(kw)
        return self.reply(kw) if callable(self.reply) else self.reply


def _votes(db, exp):
    return db.query(ExperimentVote).filter_by(experiment_id=exp.id).all()


def test_the_world_model_votes_as_one_identity_of_its_own(db):
    """what: the voter account; check: created once, an agent, owned by no
    one; expected: a distinct identity in the tally (one owner's agents
    collapse into that owner), at agent weight."""
    a = ThoughtExperimentService.ensure_world_model_voter(db)
    b = ThoughtExperimentService.ensure_world_model_voter(db)
    assert a.id == b.id and a.username == WORLD_MODEL_VOTER
    assert a.user_type == 'agent' and a.owner_id is None


@pytest.mark.parametrize('vote', [1, -1])
def test_the_world_models_measured_vote_is_cast(db, vote):
    """what: an open experiment the world model has a measured opinion on;
    expected: one vote by the world-model identity, that sign, as an agent,
    with the confidence the measurement carried."""
    e = _experiment(db)
    bridge = _Bridge({'vote': vote, 'confidence': 0.6, 'error': 0.2,
                      'typical_error': 0.1, 'log_surprise': 0.69 * vote})
    cast = ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    assert cast == 1
    (v,) = _votes(db, e)
    wm = ThoughtExperimentService.ensure_world_model_voter(db)
    assert (v.voter_id, v.vote_value, v.voter_type) == (wm.id, vote, 'agent')
    assert abs(v.confidence - 0.6) < 1e-9


@pytest.mark.parametrize('reply', [
    None,  # 503: a learn held the learner, or the world model is unreachable
    {'error': None, 'vote': 0, 'reason': 'no typical error yet'},
    {'error': None, 'vote': 0, 'reason': 'unmeasurable error'},
])
def test_no_measured_opinion_casts_nothing_and_is_retried(db, reply):
    """what: busy, unreachable or abstaining; expected: no vote now, and the
    next tick asks again (an abstention is not a vote of 0)."""
    e = _experiment(db)
    first = _Bridge(reply)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=first) == 0
    assert _votes(db, e) == [] and len(first.calls) == 1
    later = _Bridge({'vote': 1, 'confidence': 0.4})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=later) == 1
    assert len(_votes(db, e)) == 1


def test_never_twice_and_only_while_voting_is_open(db):
    """what: experiments in every status; expected: only discussing/voting
    ones are scored (cast_vote refuses the rest), and an experiment the
    world model already voted on is never scored again."""
    open_ = [_experiment(db, status=s) for s in ('discussing', 'voting')]
    closed = [_experiment(db, status=s) for s in ('proposed', 'evaluating', 'decided', 'archived')]
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    scored = {c['context'] for c in bridge.calls}
    assert scored == {e.hypothesis for e in open_}
    assert not scored & {e.hypothesis for e in closed}
    again = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=again) == 0
    assert again.calls == []


def test_a_claim_is_scored_at_its_own_time_in_utc(db):
    """what: the timestamp sent; expected: the experiment's created_at read
    as UTC epoch seconds (the column is naive UTC; .timestamp() on it would
    shift it by the box's offset, +5:30 here), and the claim is hypothesis
    -> expected outcome."""
    e = _experiment(db)
    bridge = _Bridge(None)
    ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    (call,) = bridge.calls
    assert call['timestamp'] == CREATED.replace(tzinfo=dt.timezone.utc).timestamp()
    assert (call['context'], call['outcome']) == (e.hypothesis, e.expected_outcome)
    assert call['creator_id'] == str(e.creator_id)


def test_a_claim_without_an_expected_outcome_is_not_scored(db):
    """what: no expected outcome; expected: nothing to measure the
    hypothesis against, so no call and no vote."""
    _experiment(db, expected=None)
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge) == 0
    assert bridge.calls == []


def test_a_tick_is_bounded_and_takes_the_oldest_first(db):
    """what: more open experiments than one tick scores; expected: at most
    the batch, oldest first, so a long backlog never stalls the daemon
    loop and every experiment is reached in turn."""
    exps = [_experiment(db, created=CREATED + dt.timedelta(minutes=i)) for i in range(7)]
    bridge = _Bridge({'vote': -1, 'confidence': 0.3})
    n = ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=3)
    assert n == 3
    assert [c['context'] for c in bridge.calls] == [e.hypothesis for e in exps[:3]]


def test_no_open_experiment_never_builds_a_bridge(db, monkeypatch):
    """what: nothing to score; expected: the world model is not contacted at
    all (the daemon calls this every loop; the bridge's default URL is the
    production world model on the owner's desktop)."""
    from integrations.agent_engine import world_model_bridge
    built = []
    monkeypatch.setattr(world_model_bridge, 'get_world_model_bridge',
                        lambda: built.append(1))
    _experiment(db, status='decided')
    assert ThoughtExperimentService.cast_world_model_votes(db) == 0
    assert built == []


# --- the bridge's half --------------------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    """A real WorldModelBridge that can reach nothing (as in
    test_decided_outcome_reaches_the_world_model): built with no
    HEVOLVEAI_API_URL and no in-process load, pointed at a closed port, its
    one HTTP call replaced."""
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    monkeypatch.setattr(WorldModelBridge, '_init_in_process', lambda self: None)

    def make(url='http://127.0.0.1:9'):
        b = WorldModelBridge()
        assert b._http_disabled
        b._http_disabled = False
        b._api_url = url
        return b
    return make


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _post_recorder(monkeypatch, reply):
    from integrations.agent_engine import world_model_bridge
    posts = []

    def _post(url, json=None, timeout=None, **kw):
        posts.append({'url': url, 'json': json, 'timeout': timeout})
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(world_model_bridge, 'pooled_post', _post)
    return posts


def test_the_bridge_reads_a_score_and_treats_503_as_retry(offline, monkeypatch):
    """what: the route's replies; expected: a 200 body comes back as the
    score; 503, any other status or a network error come back as None
    (retry later); the request carries the claim, its time and the
    reality bit the decide feed learns a claim with."""
    import requests
    b = offline()
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1, 'confidence': 0.5}))
    out = b.score_claim(context='h', outcome='o', timestamp=1_790_000_000.0, creator_id='u1')
    assert out == {'vote': 1, 'confidence': 0.5}
    body = posts[0]['json']
    assert posts[0]['url'].endswith('/v1/world_model/score')
    assert (body['context'], body['outcome'], body['timestamp']) == ('h', 'o', 1_790_000_000.0)
    assert body['reality_grounded'] is True
    for reply in (_Resp(503), _Resp(500), requests.ConnectionError('down')):
        _post_recorder(monkeypatch, reply)
        assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None


def test_a_cloud_world_model_scores_only_with_the_creators_consent(offline, monkeypatch):
    """what: a non-local world model, the creator has not consented;
    expected: nothing sent (the decide feed's rule for the same texts)."""
    b = offline('https://world-model.example.invalid')
    monkeypatch.setattr(b, '_has_cloud_consent', lambda uid: False)
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1}))
    assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None
    assert posts == []


def test_a_claim_is_screened_before_it_leaves(offline, monkeypatch):
    """what: a key pasted into a hypothesis; expected: redacted before it is
    sent (the screen the decide feed applies, _screened_text)."""
    b = offline()
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': -1}))
    key = 'sk-ant-' + 'A1b2C3d4' * 6
    b.score_claim(context=f'Rotate {key} weekly', outcome='fewer leaks',
                  timestamp=1.0, creator_id='u1')
    assert key not in repr(posts[0]['json'])


def test_the_daemon_tick_lets_the_world_model_vote(monkeypatch):
    """what: the agent daemon's thought-experiment tick; expected: it asks
    the world model to vote on open experiments on every loop, beside the
    lifecycle advance it already runs, and a failure there never stops it."""
    from unittest.mock import MagicMock, patch
    from integrations.agent_engine.agent_daemon import AgentDaemon
    daemon = AgentDaemon.__new__(AgentDaemon)
    daemon._auto_evolve_interval_s = 0
    daemon._next_auto_evolve_at = 0.0
    db_context = MagicMock()
    db_context.__enter__.return_value = MagicMock()
    db_context.__exit__.return_value = False
    with patch('integrations.social.models.db_session', return_value=db_context), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'advance_due_experiments'), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'cast_world_model_votes', side_effect=RuntimeError('boom')) as voter, patch(
            'integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as orch:
        orch.return_value.reconcile.return_value = {'status': 'idle'}
        daemon._advance_thought_experiments()
    assert voter.call_count == 1
    assert orch.return_value.reconcile.call_count == 1, "a voting failure stopped the tick"
