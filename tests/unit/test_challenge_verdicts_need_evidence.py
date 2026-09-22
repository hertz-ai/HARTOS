"""A challenge verdict scores fraud only on EVIDENCE of badness.

Owner rule: security must not partition the system; refuse on evidence.
Measured on central 2026-09-22 (fraud_alerts, grouped by reason):

    Challenge timeout: code_hash_check        5,451 alerts, 1,049 nodes
    Challenge timeout: stats_probe            5,446
    Challenge timeout: guardrail_verify       5,376
    Challenge timeout: agent_count_verify     5,349
    Challenge failed: Code hash changed ...   1,977 alerts,    25 nodes

21,622 of 23,840 alerts were TIMEOUTS: an unreachable peer (private 10.x
address, NAT, offline) earned +5 per challenge type, +20 per round against a
-2 decay, and was banned inside five rounds. 73 nodes were banned that way;
72 of them are dead rows now. The other 1,977 were bundled desktops whose
code hash is sha256(exe|mtime): never in the release registry, so every
reinstall or update read as tampering forever after.

The contract now:
  timeout                       -> status 'timeout', nothing scored
  new hash in the registry      -> passed, baseline advanced (unchanged)
  known old hash -> unknown new -> failed, scored, proof revoked (unchanged:
                                   the node LEFT the signed set, that is
                                   evidence)
  unknown old -> unknown new    -> 'inconclusive': proof revoked, baseline
                                   advanced, nothing scored, no 'verified'

Run:
  pytest tests/unit/test_challenge_verdicts_need_evidence.py -q --noconftest
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OLD = 'ab' * 32
NEW = 'cd' * 32


@pytest.fixture(autouse=True)
def _answers_come_from_the_target():
    """Every answer here is the TARGET's own; these tests judge its content.

    Whether an answer is bound to the target (#140) is its own contract,
    pinned with real keys in test_challenge_answer_is_bound_to_the_target.py.
    """
    from integrations.social.integrity_service import IntegrityService
    with patch.object(IntegrityService, '_answer_not_from_target',
                      return_value=None):
        yield


def a_peer(code_hash=OLD, status='verified'):
    p = MagicMock()
    p.code_hash = code_hash
    p.integrity_status = status
    p.agent_count = 0
    p.fraud_score = 0.0
    return p


def _challenge(ctype='code_hash_check', nonce='n1'):
    ch = MagicMock()
    ch.status = 'pending'
    ch.challenge_type = ctype
    ch.challenge_nonce = nonce
    ch.challenge_data = {}
    ch.target_node_id = 'node_x'
    return ch


def _registry(known):
    """A release-hash registry that knows exactly the given hashes."""
    reg = MagicMock()
    reg.is_known_release_hash.side_effect = lambda h: h in known
    return reg


def _evaluate(peer, known, reported=NEW):
    """Run evaluate_challenge_response for a code_hash_check with fraud
    scoring spied, returning (result, increase_spy, decrease_spy)."""
    from integrations.social.integrity_service import IntegrityService
    ch = _challenge()
    db = MagicMock()
    # first() order inside evaluate_challenge_response: the challenge row,
    # the peer for the code_hash_check branch, the peer for the verdict,
    # and (on revoke) the peer again inside _revoke_proof.
    db.query.return_value.filter_by.return_value.first.side_effect = [ch, peer, peer, peer]
    with patch('security.release_hash_registry.get_release_hash_registry',
               return_value=_registry(known)):
        with patch.object(IntegrityService, 'increase_fraud_score') as inc, \
                patch.object(IntegrityService, 'decrease_fraud_score') as dec:
            res = IntegrityService.evaluate_challenge_response(
                db, 'chal_1', {'nonce': 'n1', 'code_hash': reported}, '')
    return res, inc, dec, ch


# -- a timeout is absence of evidence ----------------------------------------

def test_a_timeout_scores_nothing():
    """THE REGRESSION TEST for 21,622 alerts: an unreachable peer is not a
    fraudulent peer."""
    from integrations.social.integrity_service import IntegrityService
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = a_peer()
    with patch('integrations.social.integrity_service.pooled_post',
               side_effect=requests.ConnectionError('unreachable')):
        with patch.object(IntegrityService, 'increase_fraud_score') as inc:
            IntegrityService.create_challenge(
                db, 'central', 'node_x', 'http://10.1.0.5:5000', 'code_hash_check')
    inc.assert_not_called()
    challenge = db.add.call_args[0][0]
    assert challenge.status == 'timeout', (
        'the row must still record the timeout: %r' % challenge.status)


# -- a changed hash is evidence only when the node LEFT the signed set ------

def test_an_unknown_hash_replacing_an_unknown_hash_is_inconclusive():
    """THE REGRESSION TEST for the 1,977: a desktop whose per-install hash
    changed is re-proved, not penalised."""
    peer = a_peer(code_hash=OLD, status='verified')
    res, inc, dec, ch = _evaluate(peer, known=set())
    assert res['passed'] is False
    assert res.get('inconclusive') is True, res
    inc.assert_not_called()
    dec.assert_not_called()
    assert peer.code_hash == NEW, 'the baseline must advance to what the node now runs'
    assert peer.integrity_status == 'claimed', (
        'proof must be withdrawn, not kept (%r)' % peer.integrity_status)
    assert ch.status == 'inconclusive'


def test_leaving_a_known_release_is_inconclusive_and_keeps_the_reference():
    """A fleet rollout looks like this from central until the registry catches
    up (it learns a release from the release-sign commit, its own manifest, or
    upgrade_orchestrator; it has no revocation list), so it is not evidence.
    The registered hash stays the reference so the check re-asks every round."""
    peer = a_peer(code_hash=OLD, status='verified')
    res, inc, dec, ch = _evaluate(peer, known={OLD})
    assert res['passed'] is False
    assert res.get('inconclusive') is True, res
    inc.assert_not_called()
    dec.assert_not_called()
    assert peer.code_hash == OLD, 'the registered reference must not move to an unregistered hash'
    assert peer.integrity_status == 'claimed', 'proof must be withdrawn'
    assert ch.status == 'inconclusive'


def test_the_registry_catching_up_turns_the_move_into_a_pass():
    """The round after the release-sign commit lands: same peer, same new
    hash, now known -> passed, baseline advanced, proof resumes."""
    peer = a_peer(code_hash=OLD, status='claimed')
    res, inc, dec, ch = _evaluate(peer, known={OLD, NEW})
    assert res['passed'] is True
    assert peer.code_hash == NEW
    assert peer.integrity_status == 'verified'


def test_an_update_to_a_known_release_still_passes():
    """Preservation: the OTA case the 2026-08-21 fix covered."""
    peer = a_peer(code_hash=OLD, status='claimed')
    res, inc, dec, ch = _evaluate(peer, known={NEW})
    assert res['passed'] is True
    inc.assert_not_called()
    dec.assert_called_once()
    assert peer.code_hash == NEW
    assert peer.integrity_status == 'verified'
    assert ch.status == 'passed'


def test_an_unchanged_hash_still_passes():
    """Preservation: same hash as before, nothing to decide."""
    peer = a_peer(code_hash=OLD, status='claimed')
    res, inc, dec, ch = _evaluate(peer, known=set(), reported=OLD)
    assert res['passed'] is True
    assert peer.integrity_status == 'verified'
    assert ch.status == 'passed'


# -- an undecided code claim withholds proof from the other challenge types --

def _stats_probe(peer, latest_hash_verdict):
    """A passing stats_probe for a peer whose newest code_hash_check row has
    the given status (None = no hash check on record)."""
    from integrations.social.integrity_service import IntegrityService
    ch = _challenge('stats_probe')
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [ch, peer, peer]
    latest = None
    if latest_hash_verdict is not None:
        latest = MagicMock()
        latest.status = latest_hash_verdict
    (db.query.return_value.filter.return_value.order_by.return_value
     .first.return_value) = latest
    with patch.object(IntegrityService, 'decrease_fraud_score') as dec:
        res = IntegrityService.evaluate_challenge_response(
            db, 'chal_2', {'nonce': 'n1', 'agent_count': 0}, '')
    return res, dec


def test_a_stats_pass_does_not_regrant_proof_while_the_hash_is_undecided():
    """hartos-94's bypass: rotate an unregistered hash at will (inconclusive,
    no penalty) and re-earn 'verified' through another challenge type."""
    peer = a_peer(code_hash=NEW, status='claimed')
    res, dec = _stats_probe(peer, latest_hash_verdict='inconclusive')
    assert res['passed'] is True, 'the stats probe itself still passes'
    dec.assert_called_once()
    assert peer.integrity_status == 'claimed', (
        'proof was re-granted through a stats probe while the code claim '
        'was undecided (%r)' % peer.integrity_status)


def test_a_stats_pass_does_not_regrant_proof_after_a_failed_hash_check():
    peer = a_peer(code_hash=OLD, status='claimed')
    res, dec = _stats_probe(peer, latest_hash_verdict='failed')
    assert peer.integrity_status == 'claimed'


def test_a_stats_pass_grants_proof_once_the_hash_check_passed_again():
    """The honest desktop's second round: baseline advanced, hash check
    passed, proof resumes."""
    peer = a_peer(code_hash=NEW, status='claimed')
    res, dec = _stats_probe(peer, latest_hash_verdict='passed')
    assert peer.integrity_status == 'verified'


def test_a_stats_pass_grants_proof_with_no_hash_check_on_record():
    """Preservation of the existing contract (test_peer_trust_requires_proof):
    a peer never hash-checked is not withheld."""
    peer = a_peer(code_hash=OLD, status='claimed')
    res, dec = _stats_probe(peer, latest_hash_verdict=None)
    assert peer.integrity_status == 'verified'


# -- the audit door agrees with the challenge door -------------------------

def test_the_audit_door_renders_the_same_verdict_as_the_challenge_door():
    """Two functions judge "is this peer's code hash acceptable": the challenge
    evaluator (code_hash_check) and IntegrityService.verify_code_hash (the
    audit door, via run_full_audit and verify_post_update). After 5e83047b5
    the first called an unregistered hash inconclusive while the second still
    scored it +30 hash_mismatch (ring review, 2026-09-23). Same input, same
    verdict, nothing scored."""
    from integrations.social.integrity_service import IntegrityService
    peer = a_peer(code_hash=NEW, status='claimed')
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = peer
    with patch('security.release_hash_registry.get_release_hash_registry',
               return_value=_registry(set())), \
            patch('security.master_key.load_release_manifest',
                  return_value={'code_hash': OLD}), \
            patch('security.master_key.verify_release_manifest', return_value=True), \
            patch.object(IntegrityService, 'increase_fraud_score') as inc:
        res = IntegrityService.verify_code_hash(db, 'node_x')
    assert res['verified'] is False
    assert res.get('inconclusive') is True, res
    inc.assert_not_called()


# -- the announce path is not a second granter of proof ---------------------

def _announce(status_before):
    """A direct, validly signed announce for an EXISTING peer row held in a
    real in-memory sqlite, with an 'inconclusive' code_hash_check on record.
    Returns the row's integrity_status afterwards."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import Base, PeerNode, IntegrityChallenge
    from integrations.social.peer_discovery import gossip
    eng = create_engine('sqlite://')
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(PeerNode(node_id='peer-x', url='http://10.9.9.9:5000', name='x',
                    status='active', integrity_status=status_before, code_hash=OLD))
    db.add(IntegrityChallenge(
        id='c1', challenger_node_id=gossip.node_id, target_node_id='peer-x',
        challenge_type='code_hash_check', challenge_nonce='n', status='inconclusive'))
    db.commit()
    payload = {'node_id': 'peer-x', 'url': 'http://10.9.9.9:5000', 'name': 'x',
               'version': '1.0.0', 'agent_count': 1, 'post_count': 0,
               'timestamp': 1, 'public_key': 'cd' * 32, 'signature': 'ef' * 32,
               'code_hash': OLD}
    with patch('security.node_integrity.verify_json_signature', return_value=True), \
            patch('security.master_key.get_enforcement_mode', return_value='soft'):
        gossip._merge_peer(db, payload)
    db.commit()
    status = db.query(PeerNode).filter_by(node_id='peer-x').first().integrity_status
    db.close()
    return status


def test_a_signed_announce_does_not_grant_proof():
    """THE SELF-REVIEW FINDING on 5e83047b5: _merge_peer wrote 'verified' on
    any valid announce signature, so the withheld grant came back on the
    node's next announce (~60 s). A signature proves identity, not code;
    proof is written by evaluate_challenge_response and by nothing else."""
    assert _announce('claimed') == 'claimed', (
        'a signed announce granted proof to a peer whose code claim is undecided')


def test_a_signed_announce_does_not_downgrade_proof():
    """Preservation: a peer that proved itself keeps its proof when it
    announces again; the announce path neither grants nor removes it."""
    assert _announce('verified') == 'verified'
