"""A decided thought experiment's outcome reaches the world model.

MEASURED (hevolveai Master 11.462 H, 2026-10-19): ThoughtExperimentService.
decide fed the outcome with ``bridge.submit_correction({...})`` -- ONE dict --
while WorldModelBridge.submit_correction requires ``original_response`` and
``corrected_response``.  Every decision raised TypeError, the bare
``except: pass`` swallowed it, and no decided experiment ever reached the
world model.

Fixing the call was not enough (independent review of 02f86684a, REJECTED):
  * it ran between decide's flush and the route's commit, holding SQLite's
    one write lock for up to the 30 s correction timeout, and it fed a
    decision that a rollback would then undo;
  * /v1/corrections keys a correction on the server's LAST sensor encoding
    (hevolveai receive_correction), i.e. whatever chat turn it saw last, so
    the world model would have learned an unrelated state -> "Rejected".
So the outcome now goes AFTER the commit (models.after_commit), as one text
transition of its own: the experiment's claim, then what was decided, in a
session no other reading shares, so what is learned is keyed on this
experiment's own words.

Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import logging
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.agent_engine import world_model_bridge  # noqa: E402
from integrations.agent_engine.world_model_bridge import (  # noqa: E402
    WorldModelBridge)
from integrations.social.models import (  # noqa: E402
    Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    yield s
    s.close()


class _Recorder:
    """Stands in for the bridge decide() reaches; records what it was given."""

    def __init__(self, fail=False):
        self.fed = []
        self.fail = fail

    def ingest_decided_experiment(self, **kwargs):
        self.fed.append(kwargs)
        if self.fail:
            raise RuntimeError('world model unreachable')


@pytest.fixture
def bridge(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(world_model_bridge, 'get_world_model_bridge',
                        lambda: rec)
    return rec


def _user(db):
    u = User(username=f'decide_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(u)
    db.flush()
    return u


def _experiment(db):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title='Cache warmup',
        hypothesis='Faster cache warmup lowers latency',
        expected_outcome='p50 latency falls by a third', status='evaluating')
    db.add(e)
    db.commit()
    return e


def test_the_decision_is_fed_once_the_commit_lands(db, bridge):
    """what: decide, then the route's commit; check: nothing is fed while
    the transaction is open, one feed after it commits, carrying the claim
    and the decision; expected: 0 then 1."""
    e = _experiment(db)
    out = ThoughtExperimentService.decide(
        db, e.id, 'Adopted: warmup cut p50 latency by 31%')
    assert out['status'] == 'decided'
    assert bridge.fed == [], 'fed while the write lock is still held'
    db.commit()
    assert len(bridge.fed) == 1
    fed = bridge.fed[0]
    assert fed['experiment_id'] == e.id
    assert fed['creator_id'] == str(e.creator_id)
    assert fed['claim'] == ('Faster cache warmup lowers latency\n'
                            'Expected outcome: p50 latency falls by a third')
    assert fed['decision'] == 'Adopted: warmup cut p50 latency by 31%'
    assert fed['decided_at'] == out['decision_rationale']['decided_at']


def test_a_decision_that_rolls_back_is_never_fed(db, bridge):
    """what: decide, then rollback; expected: nothing fed, ever."""
    e = _experiment(db)
    ThoughtExperimentService.decide(db, e.id, 'Adopted')
    db.rollback()
    db.commit()
    assert bridge.fed == []


def test_no_voter_identity_or_suggestion_is_fed(db, bridge):
    """what: three voters, one with free text; check: everything handed to
    the bridge; expected: no voter id and no suggestion text in it."""
    e = _experiment(db)
    voters = [_user(db) for _ in range(3)]
    for u, value in zip(voters, (2, 2, -1)):
        db.add(ExperimentVote(
            experiment_id=e.id, voter_id=u.id, voter_type='human',
            vote_value=value, confidence=1.0,
            suggestion='call me on 555-0100' if value < 0 else ''))
    db.commit()
    ThoughtExperimentService.decide(db, e.id, 'Adopted')
    db.commit()
    sent = repr(bridge.fed)
    for u in voters:
        assert str(u.id) not in sent, 'a voter identity left with the outcome'
    assert '555-0100' not in sent, 'a voter suggestion left with the outcome'


def test_an_unreachable_world_model_never_costs_the_decision(db, monkeypatch):
    """what: the bridge raises after commit; expected: the decision stays."""
    monkeypatch.setattr(world_model_bridge, 'get_world_model_bridge',
                        lambda: _Recorder(fail=True))
    e = _experiment(db)
    ThoughtExperimentService.decide(db, e.id, 'Rejected')
    db.commit()
    db.refresh(e)
    assert (e.status, e.decision_outcome) == ('decided', 'Rejected')


@pytest.fixture
def offline(monkeypatch):
    """A real WorldModelBridge that can reach nothing.  Built with no
    HEVOLVEAI_API_URL and no in-process load, so its constructor disables
    HTTP and starts no inbound poller (with the variable set, that poller
    GETs the URL every 10 s -- on the owner's desktop, production :8000;
    review of aa37aa4ab).  The tests below replace ingest_sensor_batch, the
    only sender, and re-enable HTTP on the instance for the code under test
    to run; the URL points at a closed port."""
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    monkeypatch.setattr(WorldModelBridge, '_init_in_process', lambda self: None)

    def make(url='http://127.0.0.1:9'):
        b = WorldModelBridge()
        assert b._http_disabled, 'the constructor must not have armed HTTP'
        b._http_disabled = False
        b._api_url = url
        return b
    return make


def _recording(bridge):
    """Replace the one sender; record what it got and on which thread."""
    sent = []

    def _ingest(readings):
        sent.append({'readings': readings,
                     'thread': threading.current_thread().name})
        return len(readings)
    bridge.ingest_sensor_batch = _ingest
    return sent


def test_the_bridge_sends_claim_then_decision_in_a_session_of_their_own(offline):
    """what: the bridge's half; check: ONE batch, two text readings in
    order, both in a session named for this decision only; expected: the
    claim pairs with the decision and with nothing else."""
    b = offline()
    sent = _recording(b)
    fut = b.ingest_decided_experiment(
        experiment_id='e1', creator_id='u1', claim='the claim',
        decision='Adopted', decided_at='2026-10-19T09:00:00')
    assert fut.result(timeout=10) == 2
    assert len(sent) == 1
    readings = sent[0]['readings']
    assert [r['data']['text'] for r in readings] == ['the claim', 'Adopted']
    assert {r['sensor_id'] for r in readings} == {
        'thought_experiment_e1_2026-10-19T09:00:00'}
    assert {r['sensor_type'] for r in readings} == {'text'}
    assert {r['data']['stream_source'] for r in readings} == {
        'thought_experiment'}


def test_the_feed_never_runs_on_the_deciding_thread(offline):
    """what: where the send runs; expected: on the bridge's flush executor,
    so a slow world model (up to 2 x 15 s on timeouts) never delays the
    admin's /decide response."""
    b = offline()
    sent = _recording(b)
    b.ingest_decided_experiment(
        experiment_id='e1', creator_id='u1', claim='c', decision='d',
        decided_at='t').result(timeout=10)
    assert sent[0]['thread'] != threading.current_thread().name
    assert sent[0]['thread'].startswith('wm_flush')


def test_secrets_in_the_claim_or_decision_never_leave(offline):
    """what: a key pasted into a hypothesis and a password in the decision
    (review of aa37aa4ab measured both posted word for word); expected: the
    redactor masked both before the send."""
    b = offline()
    sent = _recording(b)
    key = 'sk-ant-' + 'A1b2C3d4' * 6
    b.ingest_decided_experiment(
        experiment_id='e1', creator_id='u1',
        claim=f'Rotate the cache key {key} weekly',
        decision='Adopted; password=hunter2hunter2', decided_at='t',
    ).result(timeout=10)
    texts = ' '.join(r['data']['text'] for r in sent[0]['readings'])
    assert key not in texts
    assert 'hunter2hunter2' not in texts


@pytest.mark.parametrize('refused', ['claim', 'decision'])
def test_a_text_the_constitutional_filter_refuses_sends_nothing(offline,
                                                               monkeypatch,
                                                               refused):
    """what: the filter refuses the claim, or the decision; expected:
    nothing queued, nothing sent, not even the other half alone
    (submit_correction's rule for what it sends; review of 0a444194f: a
    mutant checking only the decision passed when only it was tested)."""
    from security.hive_guardrails import ConstitutionalFilter
    monkeypatch.setattr(
        ConstitutionalFilter, 'check_prompt',
        staticmethod(lambda text: (text != 'refused text', 'blocked')))
    texts = {'claim': 'fine claim', 'decision': 'fine decision'}
    texts[refused] = 'refused text'
    b = offline()
    sent = _recording(b)
    assert b.ingest_decided_experiment(
        experiment_id='e1', creator_id='u1', decided_at='t', **texts) is None
    assert sent == []


def test_a_cloud_world_model_needs_the_creators_consent(offline, monkeypatch):
    """what: a non-local target, the creator has not consented; expected:
    nothing queued, nothing sent."""
    b = offline('https://world-model.example.invalid')
    monkeypatch.setattr(b, '_has_cloud_consent', lambda uid: False)
    sent = _recording(b)
    assert b.ingest_decided_experiment(
        experiment_id='e1', creator_id='u1', claim='c', decision='d',
        decided_at='t') is None
    assert sent == []


def test_a_feed_that_does_not_arrive_is_said(offline, caplog):
    """what: the world model accepts fewer readings than were sent (circuit
    open, HTTP refused); expected: a warning naming the experiment."""
    b = offline()
    b.ingest_sensor_batch = lambda readings: 0
    with caplog.at_level(logging.WARNING):
        fut = b.ingest_decided_experiment(
            experiment_id='e9', creator_id='u1', claim='c', decision='d',
            decided_at='t')
        assert fut.result(timeout=10) == 0
    assert any('e9' in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records)
