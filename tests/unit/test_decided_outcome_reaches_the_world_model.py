"""A decided thought experiment's outcome reaches the world model.

MEASURED (hevolveai Master 11.462 H, 2026-10-19): ThoughtExperimentService.
decide fed the outcome with ``bridge.submit_correction({...})`` -- ONE dict --
while WorldModelBridge.submit_correction requires ``original_response`` and
``corrected_response``.  Every decision raised TypeError, the bare
``except: pass`` swallowed it, and no decided experiment ever reached the
world model, so the loop that is meant to learn from its own evolution never
learned from a single outcome.

The fake bridge below binds every call against the REAL signature, so a call
the real bridge would refuse fails here too.

Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import inspect
import os
import uuid
from unittest import mock

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
    ThoughtExperimentService)

_BRIDGE = 'integrations.agent_engine.world_model_bridge.get_world_model_bridge'
_SIGNATURE = inspect.signature(WorldModelBridge.submit_correction)


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


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


class _Bridge:
    """Records each call as the real signature binds it."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def submit_correction(self, *args, **kwargs):
        bound = _SIGNATURE.bind(self, *args, **kwargs)
        self.calls.append(bound.arguments)
        if self.fail:
            raise RuntimeError('world model unreachable')
        return {'success': True}


def test_the_decision_reaches_the_world_model(db):
    """what: one decision; check: the bridge call binds to the real signature
    and carries the claim and the outcome; expected: exactly one call."""
    e = _experiment(db)
    bridge = _Bridge()
    with mock.patch(_BRIDGE, return_value=bridge):
        out = ThoughtExperimentService.decide(
            db, e.id, 'Adopted: warmup cut p50 latency by 31%')
    assert out['status'] == 'decided'
    assert len(bridge.calls) == 1, 'the outcome is fed exactly once'
    call = bridge.calls[0]
    assert call['original_response'] == 'p50 latency falls by a third'
    assert call['corrected_response'] == 'Adopted: warmup cut p50 latency by 31%'
    assert call['explanation'] == 'Faster cache warmup lowers latency'
    assert call['context']['type'] == 'thought_experiment_outcome'
    assert call['context']['experiment_id'] == e.id


def test_no_voter_identity_or_suggestion_leaves_with_the_outcome(db):
    """what: a decided experiment with three voters, one with a free-text
    suggestion; check: the context sent carries the aggregate tally only;
    expected: no voter id and no suggestion text anywhere in it."""
    e = _experiment(db)
    voters = [_user(db) for _ in range(3)]
    for u, value in zip(voters, (2, 2, -1)):
        db.add(ExperimentVote(
            experiment_id=e.id, voter_id=u.id, voter_type='human',
            vote_value=value, confidence=1.0,
            suggestion='call me on 555-0100' if value < 0 else ''))
    db.commit()
    bridge = _Bridge()
    with mock.patch(_BRIDGE, return_value=bridge):
        ThoughtExperimentService.decide(db, e.id, 'Adopted')
    sent = repr(bridge.calls[0]['context'])
    for u in voters:
        assert str(u.id) not in sent, 'a voter identity left with the outcome'
    assert '555-0100' not in sent, 'a voter suggestion left with the outcome'
    tally = bridge.calls[0]['context']['tally']
    assert (tally['distinct_voters'], tally['distinct_supporters']) == (3, 2)
    assert tally['quorum_met'] is True


def test_an_unreachable_world_model_never_costs_the_decision(db):
    """what: the bridge raises; check: decide still records the decision;
    expected: status decided, outcome stored."""
    e = _experiment(db)
    with mock.patch(_BRIDGE, return_value=_Bridge(fail=True)):
        out = ThoughtExperimentService.decide(db, e.id, 'Rejected')
    assert out['status'] == 'decided'
    assert out['decision_outcome'] == 'Rejected'
