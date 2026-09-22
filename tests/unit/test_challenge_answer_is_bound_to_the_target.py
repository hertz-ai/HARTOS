"""A challenge answer proves something about the TARGET only if the target signed it (#140).

Measured on central 2026-09-22 20:17-20:20Z (read-only SQL): ~110 peer rows
at url localhost:6777 (central itself) were challenged while alive, and
central answered for them. 76 such challenges PASSED in six hours, and one
node (e4fa6dc8) was scored +15 four times on central's own stats.

The cause: evaluate_challenge_response verified the signature against
response_data['public_key'], the key the RESPONDER sends, so any node
answering at a row's address could sign with its own key and pass for the
row's identity. A response with no signature skipped the check entirely,
and a raising verifier was swallowed. Since 61c8ce4a6, 'verified' is
written only by an answered challenge, so the answer must be bound to the
identity it credits.

The contract these tests pin (the wrong-key row is cc12's correction):
  signed by the target's STORED key       -> passes (unchanged)
  signed by the stored key, bad content   -> failed, scored: the only case
                                             where the evidence was produced
                                             BY the target
  signed by another key, even one it sent -> inconclusive, NOT scored: it is
                                             evidence that someone else
                                             answered at the target's address,
                                             not evidence about the target.
                                             Scoring it would let anyone who
                                             can point the target's url at
                                             themselves drive an honest node
                                             to a ban.
  unsigned / target has no stored key /
  the verifier raises                     -> inconclusive: not passed, not
                                             scored, no 'verified'

Real in-memory sqlite, real Ed25519 keys, the real IntegrityService.

    pytest tests/unit/test_challenge_answer_is_bound_to_the_target.py -q
"""
import os
import sys
import uuid
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TARGET = 'target-node-0001'


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub


def _sign(priv, payload):
    from security.node_integrity import canonical_payload
    return priv.sign(canonical_payload(payload, exclude=('signature',))).hex()


@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import PeerNode, IntegrityChallenge, FraudAlert
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    for model in (PeerNode, IntegrityChallenge, FraudAlert):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _seed(db, stored_key):
    from integrations.social.models import PeerNode, IntegrityChallenge
    db.add(PeerNode(id=str(uuid.uuid4()), node_id=TARGET,
                    url='http://10.0.0.5:6777', status='active',
                    public_key=stored_key, integrity_status='unverified',
                    fraud_score=0.0, agent_count=0))
    ch = IntegrityChallenge(id=str(uuid.uuid4()), challenger_node_id='central',
                            target_node_id=TARGET,
                            challenge_type='agent_count_verify',
                            challenge_nonce='nonce-1', challenge_data={},
                            status='pending')
    db.add(ch)
    db.commit()
    return ch.id


def _answer(key_it_carries):
    return {'nonce': 'nonce-1', 'node_id': TARGET, 'agent_count': 0,
            'public_key': key_it_carries}


def _state(db):
    from integrations.social.models import PeerNode, IntegrityChallenge
    peer = db.query(PeerNode).filter_by(node_id=TARGET).one()
    ch = db.query(IntegrityChallenge).filter_by(target_node_id=TARGET).one()
    return ch.status, peer.integrity_status, float(peer.fraud_score or 0.0)


def _evaluate(db, cid, response, signature):
    from integrations.social.integrity_service import IntegrityService
    result = IntegrityService.evaluate_challenge_response(db, cid, response, signature)
    db.commit()
    return result


def test_an_answer_signed_by_the_targets_stored_key_passes(db):
    priv, pub = _keypair()
    cid = _seed(db, stored_key=pub)
    resp = _answer(pub)
    result = _evaluate(db, cid, resp, _sign(priv, resp))
    assert result['passed'] is True
    assert _state(db)[1] == 'verified'


def test_an_answer_signed_by_another_node_with_its_own_key_does_not_pass(db):
    """Central, or anyone at the row's address, answering for the target."""
    _, target_pub = _keypair()
    other_priv, other_pub = _keypair()
    cid = _seed(db, stored_key=target_pub)
    resp = _answer(other_pub)            # carries the RESPONDER's key
    result = _evaluate(db, cid, resp, _sign(other_priv, resp))
    status, integrity, fraud = _state(db)
    assert result['passed'] is False
    assert integrity != 'verified'
    assert status == 'inconclusive'
    assert fraud == 0.0, 'someone else answering must not frame the target'


def test_the_targets_own_signed_answer_that_fails_the_check_is_still_scored(db):
    """Proves the binding did not neuter real detection."""
    priv, pub = _keypair()
    cid = _seed(db, stored_key=pub)
    resp = dict(_answer(pub), nonce='wrong-nonce')
    result = _evaluate(db, cid, resp, _sign(priv, resp))
    status, integrity, fraud = _state(db)
    assert result['passed'] is False
    assert integrity != 'verified'
    assert fraud > 0.0


def test_an_unsigned_answer_is_inconclusive(db):
    _, pub = _keypair()
    cid = _seed(db, stored_key=pub)
    result = _evaluate(db, cid, _answer(''), '')
    status, integrity, fraud = _state(db)
    assert result['passed'] is False
    assert status == 'inconclusive'
    assert integrity != 'verified'
    assert fraud == 0.0


def test_an_answer_that_omits_the_key_is_judged_by_the_stored_key(db):
    priv, pub = _keypair()
    cid = _seed(db, stored_key=pub)
    resp = {'nonce': 'nonce-1', 'node_id': TARGET, 'agent_count': 0}
    result = _evaluate(db, cid, resp, _sign(priv, resp))
    assert result['passed'] is True


def test_a_target_with_no_stored_key_cannot_be_proven(db):
    priv, pub = _keypair()
    cid = _seed(db, stored_key=None)
    resp = _answer(pub)
    result = _evaluate(db, cid, resp, _sign(priv, resp))
    status, integrity, fraud = _state(db)
    assert result['passed'] is False
    assert status == 'inconclusive'
    assert integrity != 'verified'
    assert fraud == 0.0


def test_a_raising_verifier_is_inconclusive_not_a_pass(db):
    priv, pub = _keypair()
    cid = _seed(db, stored_key=pub)
    resp = _answer(pub)
    with patch('security.node_integrity.verify_json_signature',
               side_effect=RuntimeError('verifier broke')):
        result = _evaluate(db, cid, resp, _sign(priv, resp))
    status, integrity, fraud = _state(db)
    assert result['passed'] is False
    assert status == 'inconclusive'
    assert integrity != 'verified'
    assert fraud == 0.0
