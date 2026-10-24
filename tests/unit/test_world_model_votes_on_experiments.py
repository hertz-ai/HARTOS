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
from integrations.social import thought_experiment_service as tes  # noqa: E402
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


@pytest.fixture(autouse=True)
def _fresh_attempts():
    """The per-process abstention count starts empty for every test."""
    tes._WORLD_MODEL_ATTEMPTS.clear()
    tes._WORLD_MODEL_REFUSAL_LOGGED.clear()
    yield
    tes._WORLD_MODEL_ATTEMPTS.clear()
    tes._WORLD_MODEL_REFUSAL_LOGGED.clear()


def _experiment(db, status='voting', expected='p50 latency falls by a third',
                created=CREATED, hypothesis=None, title='Cache warmup'):
    creator = User(username=f'wmv_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(creator)
    db.flush()
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=creator.id, title=title,
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
    assert a.settings == {'world_model_voter': True, 'dispatchable': False}


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
    expected: nothing sent (the decide feed's rule for the same texts), and
    the claim abstains with its reason."""
    b = offline('https://world-model.example.invalid')
    monkeypatch.setattr(b, '_has_cloud_consent', lambda uid: False)
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1}))
    out = b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1')
    assert out == {'vote': 0, 'refused': True, 'reason': 'no_cloud_consent'}
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
    with patch('integrations.agent_engine.dispatch.should_yield_to_user',
               return_value=False), patch(
            'integrations.social.models.db_session', return_value=db_context), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'advance_due_experiments'), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'cast_world_model_votes', side_effect=RuntimeError('boom')) as voter, patch(
            'integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as orch:
        orch.return_value.reconcile.return_value = {'status': 'idle'}
        daemon._advance_thought_experiments()
        daemon._world_model_vote_thread.join(10)
    assert voter.call_count == 1
    assert orch.return_value.reconcile.call_count == 1, "a voting failure stopped the tick"


def test_a_tick_never_scores_after_a_write(db):
    """what: the order of database writes and score calls in one tick;
    expected: no INSERT or UPDATE before the last score. A flushed row holds
    SQLite's one write lock until the caller commits, so a score (an HTTP
    call of seconds) made after the first cast vote, or after creating the
    voter account, would hold every other writer off for the whole call."""
    from sqlalchemy import event
    exps = [_experiment(db, created=CREATED + dt.timedelta(minutes=i)) for i in range(3)]
    writes = []

    def _seen(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
            writes.append(statement.split()[0])
    event.listen(db.get_bind(), 'before_cursor_execute', _seen)
    try:
        def _reply(kw):
            assert writes == [], ("a score ran after a write", writes)
            return {'vote': 1, 'confidence': 0.5}
        bridge = _Bridge(_reply)
        assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge) == 3
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', _seen)
    assert len(bridge.calls) == 3 and writes, "the votes were written, after the scores"


# --- review of 9ca2be81a ------------------------------------------------------

def test_blank_outcomes_never_block_newer_experiments(db):
    """what: more blank-outcome experiments than one tick scores, all older
    than a real one; expected: the real one is scored in the first tick (a
    blank outcome is filtered before the batch is cut, not after)."""
    for i in range(3):
        _experiment(db, expected='   ', created=CREATED + dt.timedelta(minutes=i))
    real = _experiment(db, created=CREATED + dt.timedelta(hours=1))
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=3) == 1
    assert [c['context'] for c in bridge.calls] == [real.hypothesis]


def test_an_experiment_that_keeps_abstaining_sinks_behind_fresh_ones(db):
    """what: a full batch of older experiments the world model abstains on,
    and a newer one; expected: the next tick scores the newer one first, so
    abstainers never hold the batch for ever (they are still asked again)."""
    old = [_experiment(db, created=CREATED + dt.timedelta(minutes=i)) for i in range(3)]
    new = _experiment(db, created=CREATED + dt.timedelta(hours=1))
    abstain = {e.hypothesis for e in old}

    def _reply(kw):
        return ({'error': None, 'vote': 0} if kw['context'] in abstain
                else {'vote': 1, 'confidence': 0.5})
    first = _Bridge(_reply)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=first, limit=3) == 0
    assert {c['context'] for c in first.calls} == abstain
    second = _Bridge(_reply)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=second, limit=3) == 1
    assert second.calls[0]['context'] == new.hypothesis
    assert len(_votes(db, new)) == 1


def test_an_experiment_agents_may_not_vote_on_is_never_scored(db):
    """what: a security-guardrail experiment (voting_rules: agents cannot
    vote; cast_vote would refuse) older than an ordinary one; expected: it
    is not scored at all, and does not take a place in the batch."""
    _experiment(db, title='Tighten the security guardrail',
                hypothesis='A stricter guardrail blocks the vulnerability')
    ok = _experiment(db, created=CREATED + dt.timedelta(minutes=5))
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=1) == 1
    assert [c['context'] for c in bridge.calls] == [ok.hypothesis]


def test_the_voter_is_never_dispatched_as_a_persona(db, monkeypatch):
    """what: the agents the daemon hands goals to; expected: an ordinary
    agent persona is there, the world model's voter is not."""
    import sys
    from integrations.coding_agent.idle_detection import IdleDetectionService
    monkeypatch.delitem(sys.modules, 'hartos.create_recipe', raising=False)
    persona = User(username=f'persona_{uuid.uuid4().hex[:6]}', user_type='agent')
    db.add(persona)
    ThoughtExperimentService.ensure_world_model_voter(db)
    db.commit()
    names = {p['username'] for p in IdleDetectionService.get_idle_agent_personas(db)}
    assert persona.username in names
    assert WORLD_MODEL_VOTER not in names


@pytest.mark.parametrize('taken', ['human', 'unmarked_agent', 'owned_agent'])
def test_a_same_named_account_never_receives_the_world_models_votes(db, taken):
    """what: the voter's username already belongs to another account (a
    human, an agent without the marker, someone's own agent carrying it);
    expected: no vote is cast as it and the world model is not contacted;
    creating the voter refuses (ensure_system_user's collision rule)."""
    if taken == 'human':
        db.add(User(username=WORLD_MODEL_VOTER, user_type='human'))
    elif taken == 'unmarked_agent':
        db.add(User(username=WORLD_MODEL_VOTER, user_type='agent'))
    else:
        owner = User(username=f'o_{uuid.uuid4().hex[:6]}', user_type='human')
        db.add(owner)
        db.flush()
        db.add(User(username=WORLD_MODEL_VOTER, user_type='agent', owner_id=owner.id,
                    settings=dict(tes.WORLD_MODEL_VOTER_SETTINGS)))
    db.commit()
    e = _experiment(db)
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge) == 0
    assert _votes(db, e) == [] and bridge.calls == []
    with pytest.raises(ValueError):
        ThoughtExperimentService.ensure_world_model_voter(db)


def test_a_machine_vote_earns_no_spark(db, monkeypatch):
    """what: spark awards; expected: the world model's votes award nothing,
    while a person's vote still earns its spark (the default is unchanged)."""
    from integrations.social.resonance_engine import ResonanceService
    awarded = []
    monkeypatch.setattr(ResonanceService, 'award_action',
                        staticmethod(lambda db_, uid, action, **kw: awarded.append((uid, action))))
    e = _experiment(db)
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge) == 1
    assert awarded == []
    person = User(username=f'p_{uuid.uuid4().hex[:6]}', user_type='human')
    db.add(person)
    db.flush()
    ThoughtExperimentService.cast_vote(db, e.id, person.id, 1)
    assert awarded == [(person.id, 'experiment_voted')]


@pytest.mark.parametrize('conf', [float('nan'), float('inf'), None, 'high'])
def test_a_vote_without_a_finite_confidence_is_not_cast(db, conf):
    """what: a reply whose confidence is not a finite number; expected: no
    vote (the tally weights by confidence), retried like an abstention."""
    e = _experiment(db)
    bridge = _Bridge({'vote': 1, 'confidence': conf})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge) == 0
    assert _votes(db, e) == []
    assert tes._WORLD_MODEL_ATTEMPTS == {e.id: 1}


def test_score_failures_never_trip_the_shared_breaker(offline, monkeypatch):
    """what: many failed scores in a row; expected: the bridge's breaker,
    shared with corrections, flushes and ingest, stays closed."""
    import requests
    b = offline()
    _post_recorder(monkeypatch, requests.ConnectionError('down'))
    for _ in range(20):
        assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None
    assert not b._cb_is_open()


def test_a_reply_that_is_not_json_is_none(offline, monkeypatch):
    """what: a 200 whose body is not JSON (a proxy page); expected: None
    (retry later), not an exception out of the daemon tick."""
    class _Html(_Resp):
        def json(self):
            raise ValueError('Expecting value: line 1 column 1 (char 0)')
    b = offline()
    _post_recorder(monkeypatch, _Html(200))
    assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None


def test_the_world_model_does_not_vote_while_the_user_is_yielded_to(monkeypatch):
    """what: the daemon tick while the user-yield gate is closed (a person
    at the desk or chatting; each score costs encodes on the slot chat
    shares); expected: no voting, and the rest of the tick still runs."""
    from unittest.mock import MagicMock, patch
    from integrations.agent_engine.agent_daemon import AgentDaemon
    daemon = AgentDaemon.__new__(AgentDaemon)
    daemon._auto_evolve_interval_s = 0
    daemon._next_auto_evolve_at = 0.0
    db_context = MagicMock()
    db_context.__enter__.return_value = MagicMock()
    db_context.__exit__.return_value = False
    with patch('integrations.agent_engine.dispatch.should_yield_to_user',
               return_value=True), patch(
            'integrations.social.models.db_session', return_value=db_context), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'advance_due_experiments'), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'cast_world_model_votes') as voter, patch(
            'integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as orch:
        orch.return_value.reconcile.return_value = {'status': 'idle'}
        daemon._advance_thought_experiments()
    assert getattr(daemon, '_world_model_vote_thread', None) is None
    assert voter.call_count == 0
    assert orch.return_value.reconcile.call_count == 1


def test_a_down_world_model_costs_one_call_per_round(db):
    """what: a round with several experiments and an unreachable (or busy)
    world model; expected: one call, then the round ends (a down world
    model must not cost a timeout per experiment); an abstention does not
    end the round (the world model answered)."""
    for i in range(3):
        _experiment(db, created=CREATED + dt.timedelta(minutes=i))
    down = _Bridge(None)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=down) == 0
    assert len(down.calls) == 1
    abstains = _Bridge({'error': None, 'vote': 0})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=abstains) == 0
    assert len(abstains.calls) == 3


def test_a_slow_round_never_blocks_the_daemon_loop(monkeypatch):
    """what: a vote round that takes long (each score is an HTTP call of
    seconds); expected: the daemon tick returns and runs the rest of its
    work while the round is still scoring, and a second tick does not
    start a second round beside it."""
    import threading
    from unittest.mock import MagicMock, patch
    from integrations.agent_engine.agent_daemon import AgentDaemon
    daemon = AgentDaemon.__new__(AgentDaemon)
    daemon._auto_evolve_interval_s = 0
    daemon._next_auto_evolve_at = 0.0
    db_context = MagicMock()
    db_context.__enter__.return_value = MagicMock()
    db_context.__exit__.return_value = False
    release = threading.Event()
    rounds = []

    def _slow(db):
        rounds.append(1)
        release.wait(10)
        return 0
    with patch('integrations.agent_engine.dispatch.should_yield_to_user',
               return_value=False), patch(
            'integrations.social.models.db_session', return_value=db_context), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'advance_due_experiments'), patch(
            'integrations.social.thought_experiment_service.ThoughtExperimentService.'
            'cast_world_model_votes', side_effect=_slow), patch(
            'integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as orch:
        orch.return_value.reconcile.return_value = {'status': 'idle'}
        try:
            daemon._advance_thought_experiments()
            first = daemon._world_model_vote_thread
            assert orch.return_value.reconcile.call_count == 1
            assert first.is_alive(), "the round finished inside the tick"
            daemon._advance_thought_experiments()
            assert daemon._world_model_vote_thread is first
            assert orch.return_value.reconcile.call_count == 2
        finally:
            release.set()
            daemon._world_model_vote_thread.join(10)
    assert rounds == [1]


def test_a_vote_confidence_that_is_no_number_counts_for_nothing(db):
    """what: cast_vote with a nan confidence (an agent tool's reply);
    expected: stored as 0.0, no confidence (max(0, min(1, nan)) was 1.0,
    a full-weight vote), through the ONE clamp the bridge uses."""
    e = _experiment(db)
    agent = User(username=f'a_{uuid.uuid4().hex[:6]}', user_type='agent')
    db.add(agent)
    db.flush()
    ThoughtExperimentService.cast_vote(db, e.id, agent.id, 1, voter_type='agent',
                                       confidence=float('nan'))
    (v,) = _votes(db, e)
    assert v.confidence == 0.0


# --- review of 4c4ba922f ------------------------------------------------------

@pytest.mark.parametrize('blank', ['\t\n', '\u3000', ' \u00a0 '])
def test_an_outcome_blank_in_any_script_is_not_scored(db, blank):
    """what: an expected outcome of tabs, newlines, an ideographic or a
    non-breaking space (SQL TRIM strips only ASCII spaces); expected: never
    scored, and it takes no place in the batch."""
    _experiment(db, expected=blank)
    real = _experiment(db, created=CREATED + dt.timedelta(minutes=5))
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=1) == 1
    assert [c['context'] for c in bridge.calls] == [real.hypothesis]


def test_a_claim_that_cannot_be_scored_never_holds_up_the_round(db):
    """what: older claims the world model refuses (screened, no consent, a
    4xx) ahead of a scorable one; expected: each abstains and the round goes
    on, so the scorable one is voted in the same round; only None (the
    world model cannot answer now) ends a round."""
    refused = [_experiment(db, created=CREATED + dt.timedelta(minutes=i)) for i in range(3)]
    ok = _experiment(db, created=CREATED + dt.timedelta(hours=1))
    no = {e.hypothesis: r for e, r in zip(refused, ('screened', 'no_cloud_consent', 'http_422'))}

    def _reply(kw):
        if kw['context'] in no:
            return {'vote': 0, 'refused': True, 'reason': no[kw['context']]}
        return {'vote': 1, 'confidence': 0.5}
    bridge = _Bridge(_reply)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=4) == 1
    assert len(bridge.calls) == 4 and len(_votes(db, ok)) == 1
    assert all(tes._WORLD_MODEL_ATTEMPTS[e.id] == 1 for e in refused)


def test_the_bridge_says_why_a_claim_was_not_scored(offline, monkeypatch):
    """what: the bridge's two kinds of no-answer; expected: a claim it
    cannot score (screened text, a 422, the route's only refusal of a
    claim) abstains with its reason; a world model that cannot answer now
    is None: 503, 5xx, network, and every other 4xx (404 from a build
    without the route, 429 from a rate limiter, 401/403 from an auth
    layer), so a misconfigured world model costs one call per round."""
    import requests
    b = offline()
    monkeypatch.setattr(b, '_screened_text', lambda text: None)
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1}))
    assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') == {
        'vote': 0, 'refused': True, 'reason': 'screened'}
    assert posts == []
    b = offline()
    _post_recorder(monkeypatch, _Resp(422, {'detail': 'context and outcome must be non-empty'}))
    assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') == {
        'vote': 0, 'refused': True,
        'reason': 'http_422: context and outcome must be non-empty'}
    validation = _Resp(422, {'detail': [{'loc': ['body'], 'msg': 'field required'}]})
    for reply in (_Resp(503), _Resp(500), _Resp(404), _Resp(429), _Resp(401),
                  _Resp(403), _Resp(400), validation, _Resp(422),
                  requests.ConnectionError('down')):
        _post_recorder(monkeypatch, reply)
        assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None


# --- review of 8014c0e4e ------------------------------------------------------

def test_a_claim_the_route_refuses_never_holds_up_the_round_end_to_end(db, offline, monkeypatch):
    """what: the REAL bridge in a round; the oldest claim answers 422 (the
    route refuses it), two newer ones are scored; expected: the round goes
    on past the refusal and both newer claims are voted (4c4ba922f ended
    the round at the 422 and voted none)."""
    from integrations.agent_engine import world_model_bridge
    bad = _experiment(db)
    good = [_experiment(db, created=CREATED + dt.timedelta(minutes=i + 1)) for i in range(2)]
    b = offline()

    def _post(url, json=None, timeout=None, **kw):
        if json['context'] == bad.hypothesis:
            return _Resp(422, {'detail': 'timestamp looks like milliseconds'})
        return _Resp(200, {'vote': 1, 'confidence': 0.5})
    monkeypatch.setattr(world_model_bridge, 'pooled_post', _post)
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=b) == 2
    assert all(len(_votes(db, e)) == 1 for e in good) and _votes(db, bad) == []


def test_a_world_model_without_the_route_costs_one_call_per_round(db, offline, monkeypatch, caplog):
    """what: a world model whose build has no score route (404) or that
    rate-limits (429); expected: one call per round, nothing cast and
    nothing logged as a refusal, as for any world model that cannot answer."""
    import logging
    caplog.set_level(logging.INFO)
    for status in (404, 429):
        for i in range(3):
            _experiment(db, created=CREATED + dt.timedelta(minutes=i))
        posts = _post_recorder(monkeypatch, _Resp(status))
        assert ThoughtExperimentService.cast_world_model_votes(db, bridge=offline()) == 0
        assert len(posts) == 1
        assert not [r for r in caplog.records if 'cannot score' in r.getMessage()]


def test_a_refused_claim_is_logged_once_not_every_round(db, caplog):
    """what: a claim the world model keeps refusing, over several rounds;
    expected: one log line naming the experiment and why, not one a round."""
    import logging
    e = _experiment(db)
    bridge = _Bridge({'vote': 0, 'refused': True, 'reason': 'no_cloud_consent'})
    with caplog.at_level(logging.INFO, logger=tes.logger.name):
        for _ in range(4):
            ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    lines = [r for r in caplog.records if 'cannot score' in r.getMessage()]
    assert len(bridge.calls) == 4
    assert len(lines) == 1 and e.id in lines[0].getMessage()
    assert 'no_cloud_consent' in lines[0].getMessage()


def test_a_blank_hypothesis_is_not_scored(db):
    """what: a hypothesis of only whitespace (the API accepts it); expected:
    not scored (in-process the provider would score an empty claim)."""
    _experiment(db, hypothesis=' \t\n\u3000 ')
    real = _experiment(db, created=CREATED + dt.timedelta(minutes=5))
    bridge = _Bridge({'vote': 1, 'confidence': 0.5})
    assert ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge, limit=1) == 1
    assert [c['context'] for c in bridge.calls] == [real.hypothesis]


# --- review of 7fa1d4aba ------------------------------------------------------

def test_a_refusal_after_a_busy_reply_is_still_logged_once(db, offline, monkeypatch, caplog):
    """what: through the REAL bridge, an experiment whose first attempt met a
    busy world model (503) and whose later attempts are refused (the
    route's 422), over several rounds; expected: the refusal logged exactly
    once, with the route's own reason; a new reason is logged again."""
    import logging
    from integrations.agent_engine import world_model_bridge
    e = _experiment(db)
    replies = [_Resp(503)] + [_Resp(422, {'detail': 'bad timestamp'})] * 3 + [
        _Resp(422, {'detail': 'context and outcome must be non-empty'})]
    monkeypatch.setattr(world_model_bridge, 'pooled_post',
                        lambda url, json=None, timeout=None, **kw: replies.pop(0))
    b = offline()
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            ThoughtExperimentService.cast_world_model_votes(db, bridge=b)
    lines = [r.getMessage() for r in caplog.records if 'cannot score' in r.getMessage()]
    assert len(lines) == 2, lines
    assert e.id in lines[0] and 'bad timestamp' in lines[0]
    assert 'non-empty' in lines[1]


def test_a_claim_blank_once_cut_to_what_is_sent_is_refused_without_a_call(offline, monkeypatch):
    """what: a hypothesis of 2001 spaces and then text (the bridge sends the
    first 2000 characters); expected: refused as blank, nothing sent,
    in-process as much as over HTTP."""
    b = offline()
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1, 'confidence': 0.5}))
    out = b.score_claim(context=' ' * 2001 + 'cache warmup lowers latency',
                        outcome='o', timestamp=1.0, creator_id='u1')
    assert out == {'vote': 0, 'refused': True, 'reason': 'blank'} and posts == []

    class _Provider:
        calls = []

        def score_claim(self, *a):
            self.calls.append(a)
            return {'vote': 1, 'confidence': 0.5}
    b._in_process, b._provider = True, _Provider()
    out = b.score_claim(context=' ' * 2001 + 'x', outcome='o', timestamp=1.0, creator_id='u1')
    assert out['reason'] == 'blank' and _Provider.calls == []


def test_the_world_models_own_abstention_is_not_logged_as_a_refusal(db, caplog):
    """what: hevolveai abstains on a claim it could score ({'vote': 0,
    'reason': ...}, no 'refused'); expected: counted and retried, never
    logged as a claim the world model cannot score."""
    import logging
    _experiment(db)
    bridge = _Bridge({'error': None, 'vote': 0, 'reason': 'no typical error yet'})
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    assert len(bridge.calls) == 3
    assert not [r for r in caplog.records if 'cannot score' in r.getMessage()]


# --- review of fe854f009 ------------------------------------------------------

def test_a_closed_experiment_leaves_no_per_experiment_state(db):
    """what: an experiment refused, then closed; expected: its attempt count
    and its last logged reason are dropped on the next round."""
    e = _experiment(db)
    ThoughtExperimentService.cast_world_model_votes(
        db, bridge=_Bridge({'vote': 0, 'refused': True, 'reason': 'screened'}))
    assert e.id in tes._WORLD_MODEL_ATTEMPTS and e.id in tes._WORLD_MODEL_REFUSAL_LOGGED
    e.status = 'evaluating'
    db.commit()
    ThoughtExperimentService.cast_world_model_votes(db, bridge=_Bridge(None))
    assert e.id not in tes._WORLD_MODEL_ATTEMPTS
    assert e.id not in tes._WORLD_MODEL_REFUSAL_LOGGED


def test_a_refusal_is_logged_when_its_reason_changes(db, caplog):
    """what: reasons A, B, B over three rounds; expected: two lines (each
    change), not three; a newline in a reason cannot forge a line."""
    import logging
    _experiment(db)
    reasons = iter(['A', 'B\nINFO forged\r\u2028\x85', 'B\nINFO forged\r\u2028\x85'])
    bridge = _Bridge(lambda kw: {'vote': 0, 'refused': True, 'reason': next(reasons)})
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    lines = [r.getMessage() for r in caplog.records if 'cannot score' in r.getMessage()]
    assert len(lines) == 2
    assert all(len(ln.splitlines()) == 1 for ln in lines)


def test_a_422_detail_is_bounded_and_a_400_is_not_a_refusal(offline, monkeypatch):
    b = offline()
    _post_recorder(monkeypatch, _Resp(422, {'detail': 'y' * 500}))
    out = b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1')
    assert out['reason'] == 'http_422: ' + 'y' * 200
    _post_recorder(monkeypatch, _Resp(400, {'detail': 'not this route'}))
    assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None


def test_an_outcome_blank_once_cut_is_refused_without_a_call(offline, monkeypatch):
    b = offline()
    posts = _post_recorder(monkeypatch, _Resp(200, {'vote': 1, 'confidence': 0.5}))
    out = b.score_claim(context='h', outcome=' ' * 2001 + 'latency falls',
                        timestamp=1.0, creator_id='u1')
    assert out['reason'] == 'blank' and posts == []


def test_a_422_without_a_reason_is_warned_once(offline, monkeypatch, caplog):
    """what: a 422 without a reason (FastAPI's validation 422); expected:
    None every time, and one warning saying why nothing votes, not one a
    round."""
    import logging
    from integrations.agent_engine import world_model_bridge
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_CONTRACT', False)
    b = offline()
    _post_recorder(monkeypatch, _Resp(422, {'detail': [{'msg': 'field required'}]}))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None
    warned = [r.getMessage() for r in caplog.records if 'rejects every request' in r.getMessage()]
    assert len(warned) == 1 and 'field required' in warned[0]


# --- review of e6e039539 ------------------------------------------------------

def test_the_routes_own_refusal_never_warns_a_broken_contract(offline, monkeypatch, caplog):
    """what: the route's own 422 refusing a claim (a string detail);
    expected: an abstention with its reason, and no "rejects every request"
    warning (which would also spend the one warning a real break gets)."""
    import logging
    from integrations.agent_engine import world_model_bridge
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_CONTRACT', False)
    b = offline()
    _post_recorder(monkeypatch, _Resp(422, {'detail': 'timestamp looks like milliseconds'}))
    with caplog.at_level(logging.WARNING):
        out = b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1')
    assert out['refused'] is True
    assert not [r for r in caplog.records if 'rejects every request' in r.getMessage()]
    assert world_model_bridge._WARNED_422_CONTRACT is False


def test_a_broken_contract_is_warned_again_after_it_recovers(offline, monkeypatch, caplog):
    """what: a reason-less 422, a real score, a reason-less 422, first within
    the re-arm floor, then after it; expected: one warning within the floor
    (a flapping proxy does not warn every round), a second after a real
    score past the floor, and the recovery said; each warning capped."""
    import logging
    from integrations.agent_engine import world_model_bridge
    clock = [1000.0]
    monkeypatch.setattr(world_model_bridge.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_CONTRACT', False)
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_AT', 0.0)
    b = offline()
    huge = {'detail': [{'msg': 'x' * 100000}]}

    def _post(reply):
        _post_recorder(monkeypatch, reply)
        b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1')
    with caplog.at_level(logging.INFO):
        for reply in (_Resp(422, huge), _Resp(200, {'vote': 1, 'confidence': 0.5}),
                      _Resp(422, huge)):
            _post(reply)
        clock[0] += world_model_bridge._WARN_422_REARM_S
        _post(_Resp(200, {'vote': 1, 'confidence': 0.5}))
        _post(_Resp(422, huge))
    msgs = [r.getMessage() for r in caplog.records]
    warned = [m for m in msgs if 'rejects every request' in m]
    assert len(warned) == 2 and all(len(w) < 500 for w in warned)
    assert sum('answers again' in m for m in msgs) == 1


def test_only_a_real_score_re_arms_the_warning(offline, monkeypatch):
    """what: after a warned break and past the floor, replies that are not
    a score (503, 404, a 200 that is not JSON, a 200 dict without a vote);
    expected: none re-arms the warning, and a non-score 200 is no score."""
    from integrations.agent_engine import world_model_bridge
    clock = [1000.0]
    monkeypatch.setattr(world_model_bridge.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_CONTRACT', False)
    monkeypatch.setattr(world_model_bridge, '_WARNED_422_AT', 0.0)
    b = offline()
    _post_recorder(monkeypatch, _Resp(422))
    b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1')
    assert world_model_bridge._WARNED_422_CONTRACT is True
    clock[0] += 10 * world_model_bridge._WARN_422_REARM_S

    class _NotJson(_Resp):
        def json(self):
            raise ValueError('not json')
    for reply in (_Resp(503), _Resp(404), _NotJson(200), _Resp(200, {'error': 'upstream'})):
        _post_recorder(monkeypatch, reply)
        assert b.score_claim(context='h', outcome='o', timestamp=1.0, creator_id='u1') is None
        assert world_model_bridge._WARNED_422_CONTRACT is True


def test_a_logged_refusal_reason_is_bounded(db, caplog):
    """what: a reply whose refusal reason is a megabyte; expected: the round
    logs a bounded line."""
    import logging
    _experiment(db)
    bridge = _Bridge({'vote': 0, 'refused': True, 'reason': 'r' * 1000000})
    with caplog.at_level(logging.INFO):
        ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    lines = [r.getMessage() for r in caplog.records if 'cannot score' in r.getMessage()]
    assert len(lines) == 1 and len(lines[0]) < 1000


def test_a_logged_reason_is_bounded_as_printed(db, caplog):
    """what: a refusal reason of characters repr() expands tenfold;
    expected: the printed line is still bounded."""
    import logging
    _experiment(db)
    bridge = _Bridge({'vote': 0, 'refused': True, 'reason': '\U000e0001' * 100000})
    with caplog.at_level(logging.INFO):
        ThoughtExperimentService.cast_world_model_votes(db, bridge=bridge)
    lines = [r.getMessage() for r in caplog.records if 'cannot score' in r.getMessage()]
    assert len(lines) == 1 and len(lines[0]) < 500
