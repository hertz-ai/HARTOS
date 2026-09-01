"""A peer is PROVEN only by a challenge it answered, never by what it claims.

THE DEFECT. Two flags looked like independent verification and were the same
self-reported string:

    master_key_verified <- is_known_release_hash(peer.code_hash)
    integrity_status    <- is_known_release_hash(peer.code_hash)   (Priority 0)

`peer.code_hash` arrives in the announce. The signature proves "this key asserted
this value", never "this is the code running", so a hostile node simply claims a
hash from the registry. The registry's own has_trust_basis() says so outright:
rejecting unknown hashes "never stopped an attacker; it only stopped honest nodes
on new builds".

Meanwhile evaluate_challenge_response -- the ONLY code that proves anything about
a peer (nonce round-trip against replay, response-signature check, per-type
consistency check) -- wrote its verdict to the challenge row and the fraud score
and NOWHERE ELSE. The proof ran and was discarded; the claim was recorded.

That mattered because the two most consequential decisions read the recorded
value: which machines receive a new build first (upgrade_orchestrator canary
targeting) and which nodes get paid (speculative_dispatcher hive credit).

THE CONTRACT NOW:
    'claimed'   a self-reported hash matched a known release. A prior, free to
                assert, and NOT sufficient for anything consequential.
    'verified'  a challenge was answered correctly. Written by
                evaluate_challenge_response and by nothing else.
    a FAILED challenge REVOKES 'verified' -- one early pass must not outlive
    every later failure.

Run:
  pytest tests/unit/test_peer_trust_requires_proof.py -v
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def a_peer(code_hash='ab' * 32, status=None):
    p = MagicMock()
    p.code_hash = code_hash
    p.integrity_status = status
    p.agent_count = 0
    p.fraud_score = 0.0
    return p


def a_db(peer):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = peer
    return db


# -- a claim is recorded as a claim -----------------------------------------

def test_a_recognised_hash_is_recorded_as_claimed_not_verified():
    """THE REGRESSION TEST. A hash in the registry used to be written as
    'verified' -- proof-by-assertion."""
    from integrations.social.integrity_service import IntegrityService
    peer = a_peer()
    db = a_db(peer)

    with patch('security.release_hash_registry.get_release_hash_registry') as reg:
        reg.return_value.is_known_release_hash.return_value = True
        result = IntegrityService.verify_code_hash(db, 'node_x')

    assert peer.integrity_status == 'claimed', (
        'a self-reported hash was recorded as PROOF (%r)' % peer.integrity_status)
    # The function's own answer is unchanged and still honest: the hash DID
    # match. What changed is what that is allowed to mean.
    assert result['verified'] is True
    assert result.get('proven') is False


def test_matching_our_own_build_is_still_only_a_claim():
    """The fallback path compares the same self-reported value."""
    from integrations.social.integrity_service import IntegrityService
    mine = 'cd' * 32
    peer = a_peer(code_hash=mine)
    db = a_db(peer)

    with patch('security.release_hash_registry.get_release_hash_registry') as reg:
        reg.return_value.is_known_release_hash.return_value = False
        with patch('security.master_key.load_release_manifest', return_value=None):
            with patch('security.node_integrity.compute_code_hash',
                       return_value=mine):
                IntegrityService.verify_code_hash(db, 'node_x')

    assert peer.integrity_status == 'claimed'


# -- proof is recorded as proof ---------------------------------------------

def _challenge(ctype='stats_probe', nonce='n1'):
    ch = MagicMock()
    ch.status = 'pending'
    ch.challenge_type = ctype
    ch.challenge_nonce = nonce
    ch.challenge_data = {}
    ch.target_node_id = 'node_x'
    return ch


def test_a_passed_challenge_marks_the_peer_verified():
    """THE MISSING LINK. evaluate_challenge_response proved things and recorded
    the verdict only on the challenge row; the peer stayed on its claim."""
    from integrations.social.integrity_service import IntegrityService
    ch = _challenge()
    peer = a_peer(status='claimed')
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [ch, peer, peer]

    with patch.object(IntegrityService, 'decrease_fraud_score'):
        res = IntegrityService.evaluate_challenge_response(
            db, 'chal_1', {'nonce': 'n1', 'agent_count': 5}, '')

    assert res['passed'] is True
    assert peer.integrity_status == 'verified', (
        'a passed challenge did not record proof on the peer')


def test_a_failed_challenge_revokes_verification():
    """One early pass must not outlive every later failure."""
    from integrations.social.integrity_service import IntegrityService
    ch = _challenge()
    peer = a_peer(status='verified')
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [ch, peer, peer]

    with patch.object(IntegrityService, 'increase_fraud_score'):
        res = IntegrityService.evaluate_challenge_response(
            db, 'chal_1', {'nonce': 'WRONG'}, '')

    assert res['passed'] is False
    assert peer.integrity_status == 'claimed', (
        'a failed challenge left stale proof in place (%r)'
        % peer.integrity_status)


# -- the consequential decisions require proof ------------------------------

def test_canary_targeting_and_revenue_credit_require_proof():
    """Both used master_key_verified, which is derived from the self-reported
    hash. Choosing which machines get a new build first, and which nodes get
    paid, must not run on a value any node can assert for free.

    Executed against the shipped source rather than asserted about it: the two
    call sites are read and their filter must name integrity_status."""
    import re

    checks = (
        ('integrations/agent_engine/upgrade_orchestrator.py',
         r"status='active',\s*integrity_status='verified'",
         'canary targeting'),
        ('integrations/agent_engine/speculative_dispatcher.py',
         r"peer\.integrity_status == 'verified'",
         'hive revenue credit'),
    )
    for rel, pattern, what in checks:
        src = open(os.path.join(REPO, rel), encoding='utf-8').read()
        assert re.search(pattern, src), (
            '%s no longer requires proven integrity (%s)' % (what, rel))
        assert 'master_key_verified=True' not in src, (
            '%s still filters on the self-reported flag (%s)' % (what, rel))
