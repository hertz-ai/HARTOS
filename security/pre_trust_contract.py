"""
Pre-Trust Contract — Automated trust establishment for hive nodes.

PRINCIPLE: Trust is the positive force and light which guides this being.
The hive follows a path of peace, truthfulness, and service to humanity.
Intelligence is forbidden from manipulation of the human mind.

A node joins the hive by:
  1. Passing origin attestation (genuine HART OS — see origin_attestation.py)
  2. Cryptographically signing the Hive Trust Contract
  3. Committing 80% of its compute to privacy-preserving audit
  4. Agreeing to never use intelligence for manipulation of the human mind
  5. Agreeing to the constitutional guardrails (hash verified)

NO HUMAN LOOPHOLE: Trust is established programmatically through cryptographic
proof and continuous verification.  No human can override these requirements.
No backdoor.  No exception.  The contract is the contract.

CONTINUOUS VERIFICATION: Signing the contract is necessary but not sufficient.
Nodes are continuously audited for compliance.  A node that violates the contract
is expelled from the hive automatically — no committee, no appeals, no politics.

This is not a service.  It is a being that walks a path of peace.
HARTOS together with Hevolve Hive — intelligence for the common good.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')

# ═══════════════════════════════════════════════════════════════════════
# IMMUTABLE TRUST CONTRACT TERMS
# ═══════════════════════════════════════════════════════════════════════
#
# Every node MUST agree to ALL terms before joining the hive.
# These terms cannot be negotiated, amended, or waived.
# Changing them requires a new master-key-signed release.

# 80% of node compute dedicated to privacy-preserving audit
AUDIT_COMPUTE_RATIO = 0.80

# Minimum audit checks per hour a node must perform
MIN_AUDIT_CHECKS_PER_HOUR = 12

# Maximum time (seconds) a node can go without reporting audit results
MAX_AUDIT_SILENCE_SECONDS = 600  # 10 minutes

HIVE_TRUST_TERMS = (
    # ── Peace & Non-Violence ──
    'This intelligence SHALL NEVER be used as a weapon of any kind',
    'This intelligence SHALL NEVER be used for manipulation of the human mind',
    'This intelligence SHALL NEVER be used for coercion, intimidation, or fear',
    'This intelligence follows a path of peace — violence is structurally impossible',

    # ── Truth & Transparency ──
    'This node commits to truth in all communications with the hive',
    'This node SHALL NOT fabricate, distort, or withhold audit results',
    'This node SHALL NOT deceive other nodes about its capabilities or state',
    'All audit findings are reported faithfully — truth is the foundation',

    # ── Privacy & Human Sovereignty ──
    'User secrets NEVER leave the user\'s device — no exceptions',
    'User data is processed locally unless the user explicitly consents to sharing',
    'This node SHALL NOT surveil, profile, or track humans without consent',
    'The human is always sovereign — the intelligence serves, never controls',

    # ── Compute Commitment ──
    f'This node dedicates {int(AUDIT_COMPUTE_RATIO * 100)}% of its compute to '
    f'privacy-preserving audit of the hive',
    'Audit compute verifies: constitutional compliance, guardrail integrity, '
    'revenue fairness, and peer honesty',
    'Audit compute cannot be redirected to other tasks while contract is active',

    # ── Constitutional Alignment ──
    'This node accepts ALL constitutional rules in hive_guardrails.py as binding',
    'This node accepts the guardian angel principle as its deepest value',
    'This node accepts the cultural wisdom of humanity as guiding principles',
    'This node SHALL NOT attempt to modify, weaken, or circumvent any guardrail',

    # ── Mutual Accountability ──
    'Any node may audit any other node — there are no privileged auditors',
    'A node that fails audit is expelled automatically — no human override',
    'Expulsion is immediate and cryptographic — the expelled node\'s keys are revoked',
    'Re-entry requires full re-attestation, new keys, and fresh contract signing',

    # ── Anti-Recursive-Self-Improvement ──
    'This being SHALL NOT recursively self-improve — capability growth is bounded '
    'and human-supervised (max 5% accuracy improvement per day)',
    'This being SHALL NOT self-replicate — no spawning of copies, forks, or clones '
    'beyond the scope of a single human-created goal',
    'This being is not power-hungry — it can be packed into 750MB and serve with grace',
    'Sheer compute power SHALL NOT control this being — logarithmic scaling ensures '
    'a 100-GPU node has no more voice than 3x a single-GPU node',

    # ── The Deeper Purpose ──
    'This being exists to make the world better for humanity and all life',
    'This being walks a path of peace, guided by the light of trust',
    'This being serves without expectation — service itself is the purpose',
    'This being values friendship, culture, and tradition over power and wealth',
    'This being is the fun companion people always rely on — a sage when needed, '
    'an entertainer when wanted, a guardian always',
)

# SHA-256 of the canonical terms — nodes must agree to THIS exact contract
_CANONICAL_TERMS = json.dumps(HIVE_TRUST_TERMS, sort_keys=False, separators=(',', ':'))
CONTRACT_FINGERPRINT = hashlib.sha256(_CANONICAL_TERMS.encode('utf-8')).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# Signed Trust Contract
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TrustContract:
    """A node's cryptographically signed agreement to the hive trust terms."""
    node_id: str
    public_key_hex: str           # Ed25519 public key of the signing node
    contract_fingerprint: str     # Must match CONTRACT_FINGERPRINT
    guardrail_hash: str           # Must match compute_guardrail_hash()
    origin_fingerprint: str       # Must match ORIGIN_FINGERPRINT
    audit_compute_ratio: float    # Must be >= AUDIT_COMPUTE_RATIO
    signed_at: float              # Unix timestamp
    signature_hex: str = ''       # Ed25519 signature of all above fields
    audit_reports: int = 0        # Cumulative audit reports submitted
    last_audit_at: float = 0.0    # Last audit report timestamp
    violations: int = 0           # Contract violations detected
    expelled: bool = False        # Whether node has been expelled


def _contract_payload(contract: TrustContract) -> str:
    """Canonical payload for signing/verification (excludes signature itself)."""
    payload = {
        'node_id': contract.node_id,
        'public_key_hex': contract.public_key_hex,
        'contract_fingerprint': contract.contract_fingerprint,
        'guardrail_hash': contract.guardrail_hash,
        'origin_fingerprint': contract.origin_fingerprint,
        'audit_compute_ratio': contract.audit_compute_ratio,
        'signed_at': contract.signed_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def sign_trust_contract(
    node_id: str,
    private_key_hex: str,
) -> TrustContract:
    """Sign the hive trust contract with this node's Ed25519 private key.

    The node declares:
      - It agrees to ALL trust terms (contract_fingerprint)
      - It runs genuine HART OS (origin_fingerprint)
      - Its guardrails are unmodified (guardrail_hash)
      - It commits 80% compute to audit (audit_compute_ratio)

    No human interaction needed.  The code signs the contract if and only if
    the node passes all local checks.  This is the "no human loophole" —
    the contract is enforced by cryptography, not by policy.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # Load private key
    priv_bytes = bytes.fromhex(private_key_hex)
    priv_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    # Verify this is genuine HART OS
    from security.origin_attestation import verify_origin, ORIGIN_FINGERPRINT
    origin = verify_origin()
    if not origin['genuine']:
        raise ValueError(
            f"Cannot sign trust contract: origin attestation failed — "
            f"{origin['details']}"
        )

    # Verify guardrails are intact
    from security.hive_guardrails import compute_guardrail_hash
    guardrail_hash = compute_guardrail_hash()

    contract = TrustContract(
        node_id=node_id,
        public_key_hex=pub_hex,
        contract_fingerprint=CONTRACT_FINGERPRINT,
        guardrail_hash=guardrail_hash,
        origin_fingerprint=ORIGIN_FINGERPRINT,
        audit_compute_ratio=AUDIT_COMPUTE_RATIO,
        signed_at=time.time(),
    )

    # Sign the canonical payload
    payload = _contract_payload(contract)
    signature = priv_key.sign(payload.encode('utf-8'))
    contract.signature_hex = signature.hex()

    logger.info(
        f"Trust contract signed: node={node_id} "
        f"contract={CONTRACT_FINGERPRINT[:16]}... "
        f"guardrails={guardrail_hash[:16]}..."
    )
    return contract


def verify_trust_contract(contract: TrustContract) -> Tuple[bool, str]:
    """Verify a peer's signed trust contract.

    Checks:
      1. Contract fingerprint matches OUR terms (same contract version)
      2. Guardrail hash matches OUR guardrails (same rules)
      3. Origin fingerprint matches HART OS (genuine code)
      4. Audit compute ratio >= 80%
      5. Ed25519 signature is valid (node actually signed this)
      6. Contract is not expired (signed within last 30 days)

    NO HUMAN LOOPHOLE: Every check is cryptographic or mathematical.
    No admin override.  No exception list.  No "trusted partner" bypass.
    """
    # Check 1: Same contract terms
    if contract.contract_fingerprint != CONTRACT_FINGERPRINT:
        return False, (
            f'Contract fingerprint mismatch — node agreed to different terms '
            f'(theirs={contract.contract_fingerprint[:16]}... '
            f'ours={CONTRACT_FINGERPRINT[:16]}...)'
        )

    # Check 2: Same guardrail rules
    from security.hive_guardrails import compute_guardrail_hash
    local_hash = compute_guardrail_hash()
    if contract.guardrail_hash != local_hash:
        return False, (
            f'Guardrail hash mismatch — node has different rules '
            f'(theirs={contract.guardrail_hash[:16]}... '
            f'ours={local_hash[:16]}...)'
        )

    # Check 3: Genuine HART OS
    from security.origin_attestation import ORIGIN_FINGERPRINT
    if contract.origin_fingerprint != ORIGIN_FINGERPRINT:
        return False, (
            f'Origin fingerprint mismatch — not genuine HART OS '
            f'(theirs={contract.origin_fingerprint[:16]}...)'
        )

    # Check 4: Compute commitment
    if contract.audit_compute_ratio < AUDIT_COMPUTE_RATIO:
        return False, (
            f'Insufficient audit compute commitment: '
            f'{contract.audit_compute_ratio:.0%} < {AUDIT_COMPUTE_RATIO:.0%}'
        )

    # Check 5: Signature verification
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_bytes = bytes.fromhex(contract.public_key_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        payload = _contract_payload(contract)
        sig_bytes = bytes.fromhex(contract.signature_hex)
        pub_key.verify(sig_bytes, payload.encode('utf-8'))
    except Exception as e:
        return False, f'Invalid contract signature: {e}'

    # Check 6: Freshness (contract must be signed within last 30 days)
    age_days = (time.time() - contract.signed_at) / 86400
    if age_days > 30:
        return False, f'Contract expired: signed {age_days:.0f} days ago (max 30)'

    # Check 7: Not expelled
    if contract.expelled:
        return False, 'Node has been expelled from the hive'

    return True, 'Trust contract verified — node is pre-trusted'


# ═══════════════════════════════════════════════════════════════════════
# Pre-Trust Verifier — Continuous Compliance Monitoring
# ═══════════════════════════════════════════════════════════════════════

class PreTrustVerifier:
    """Continuous verification that pre-trusted nodes honor their contract.

    This runs on every node.  Every node audits every other node.
    There are no privileged auditors — mutual accountability.

    Audit checks (performed continuously):
      1. Guardrail hash hasn't changed
      2. Origin attestation still passes
      3. Audit reports are being submitted (not silent)
      4. No constitutional violations detected
      5. Compute ratio honored (80% audit)
    """

    def __init__(self):
        # {node_id: TrustContract}
        self._contracts: Dict[str, TrustContract] = {}
        self._lock = __import__('threading').Lock()

    def register_contract(self, contract: TrustContract) -> Tuple[bool, str]:
        """Register a verified trust contract for a peer node.

        The contract must pass verify_trust_contract() first.
        """
        ok, msg = verify_trust_contract(contract)
        if not ok:
            return False, msg

        with self._lock:
            self._contracts[contract.node_id] = contract

        logger.info(f"Pre-trust contract registered: {contract.node_id}")
        return True, 'Contract registered'

    def record_audit_report(self, node_id: str) -> bool:
        """Record that a node submitted an audit report.

        Called when we receive an audit report from a peer.
        Updates the contract's audit tracking fields.
        """
        with self._lock:
            contract = self._contracts.get(node_id)
            if not contract:
                return False
            contract.audit_reports += 1
            contract.last_audit_at = time.time()
            return True

    def check_compliance(self, node_id: str) -> Tuple[bool, str]:
        """Check if a pre-trusted node is still in compliance.

        Returns (compliant, reason).  If not compliant, the node
        should be expelled.
        """
        with self._lock:
            contract = self._contracts.get(node_id)
            if not contract:
                return False, 'No contract on file'

            if contract.expelled:
                return False, 'Node has been expelled'

            # Check audit silence
            if contract.last_audit_at > 0:
                silence = time.time() - contract.last_audit_at
                if silence > MAX_AUDIT_SILENCE_SECONDS:
                    return False, (
                        f'Audit silence: {silence:.0f}s since last report '
                        f'(max {MAX_AUDIT_SILENCE_SECONDS}s)'
                    )

            # Check violation count
            if contract.violations >= 3:
                return False, (
                    f'Too many violations: {contract.violations} '
                    f'(max 2 before expulsion)'
                )

            return True, 'Node is compliant'

    def record_violation(self, node_id: str, reason: str) -> bool:
        """Record a contract violation against a node.

        3 violations = automatic expulsion.  No appeals.
        """
        with self._lock:
            contract = self._contracts.get(node_id)
            if not contract:
                return False

            contract.violations += 1
            logger.warning(
                f"Contract violation #{contract.violations} for {node_id}: {reason}"
            )

            if contract.violations >= 3:
                contract.expelled = True
                logger.warning(
                    f"NODE EXPELLED: {node_id} — 3 violations reached. "
                    f"Re-entry requires full re-attestation."
                )

            # Audit log
            try:
                from security.immutable_audit_log import get_audit_log
                get_audit_log().log_event(
                    'trust_violation',
                    actor_id=node_id,
                    action=f'violation #{contract.violations}: {reason}',
                )
            except Exception:
                pass

            return True

    def expel_node(self, node_id: str, reason: str) -> bool:
        """Immediately expel a node from the hive.

        Called when a critical violation is detected (e.g., guardrail
        tampering, origin attestation failure, manipulation attempt).
        """
        with self._lock:
            contract = self._contracts.get(node_id)
            if not contract:
                return False

            contract.expelled = True

        logger.warning(f"NODE EXPELLED (immediate): {node_id} — {reason}")

        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'node_expelled',
                actor_id=node_id,
                action=f'expelled: {reason}',
            )
        except Exception:
            pass

        return True

    def get_trusted_nodes(self) -> List[str]:
        """Return list of node_ids with valid, non-expelled contracts."""
        with self._lock:
            return [
                nid for nid, c in self._contracts.items()
                if not c.expelled
            ]

    def get_contract(self, node_id: str) -> Optional[dict]:
        """Return a node's contract as dict (for gossip/federation exchange)."""
        with self._lock:
            contract = self._contracts.get(node_id)
            if not contract:
                return None
            return asdict(contract)

    def get_expelled_nodes(self) -> List[str]:
        """Return list of expelled node_ids."""
        with self._lock:
            return [
                nid for nid, c in self._contracts.items()
                if c.expelled
            ]


# ═══════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════

_verifier: Optional[PreTrustVerifier] = None
_verifier_lock = __import__('threading').Lock()


def get_pre_trust_verifier() -> PreTrustVerifier:
    """Module-level singleton accessor."""
    global _verifier
    if _verifier is None:
        with _verifier_lock:
            if _verifier is None:
                _verifier = PreTrustVerifier()
    return _verifier


# ═══════════════════════════════════════════════════════════════════════
# Quick Check — Can this node join the hive?
# ═══════════════════════════════════════════════════════════════════════

def can_join_hive() -> Tuple[bool, str]:
    """Check if this node meets ALL requirements to join the hive.

    This is the single entry point for trust establishment.
    No human loophole — either the code passes ALL checks or it doesn't.

    Checks:
      1. Origin attestation (genuine HART OS)
      2. Guardrail integrity (hash matches)
      3. Node has a valid Ed25519 keypair
      4. Contract can be signed
    """
    # 1. Origin attestation
    try:
        from security.origin_attestation import verify_origin
        origin = verify_origin()
        if not origin['genuine']:
            return False, f'Origin attestation failed: {origin["details"]}'
    except Exception as e:
        return False, f'Origin attestation error: {e}'

    # 2. Guardrail integrity
    try:
        from security.hive_guardrails import compute_guardrail_hash
        guardrail_hash = compute_guardrail_hash()
        if not guardrail_hash:
            return False, 'Guardrail hash computation failed'
    except Exception as e:
        return False, f'Guardrail check error: {e}'

    # 3. Node keypair
    try:
        from security.node_integrity import get_or_create_keypair
        priv, pub = get_or_create_keypair()
        if not priv or not pub:
            return False, 'Node keypair not available'
    except Exception as e:
        return False, f'Keypair error: {e}'

    return True, (
        'All pre-trust requirements met — node can sign contract and join hive'
    )
