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


def test_leaving_a_known_release_is_still_a_failure():
    """Preservation: a node that was on a signed release and now is not."""
    from integrations.social.integrity_service import FRAUD_WEIGHTS
    peer = a_peer(code_hash=OLD, status='verified')
    res, inc, dec, ch = _evaluate(peer, known={OLD})
    assert res['passed'] is False
    assert not res.get('inconclusive')
    inc.assert_called_once()
    assert inc.call_args[0][2] == FRAUD_WEIGHTS['challenge_fail']
    assert peer.code_hash == OLD, 'a failed check must not advance the baseline'
    assert peer.integrity_status == 'claimed'
    assert ch.status == 'failed'


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
