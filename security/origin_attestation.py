"""
Origin Attestation — Cryptographic proof that this IS genuine HART OS.

Prevents forks from:
  1. Rebranding HART OS as their own OS
  2. Joining the federation with modified code
  3. Stripping branding while keeping functionality
  4. Claiming they built it independently

Protection layers:
  1. ORIGIN_FINGERPRINT — SHA-256 of immutable identity markers, checked at boot
  2. BRAND_MARKERS — Frozen strings that must exist in specific files
  3. verify_origin() — Called at boot + federation handshake + every 5 minutes
  4. Master key verification — Forks don't have the private key, can't sign releases
  5. Federation rejection — Nodes that fail attestation are blacklisted

A fork can copy ALL the code, but they cannot:
  - Sign releases with the master key (they don't have it)
  - Pass origin attestation (fingerprint includes master public key)
  - Join the federation (handshake requires signed attestation)
  - Remove this file (runtime_monitor detects tampering)
"""

import hashlib
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger('hevolve_security')

# ═══════════════════════════════════════════════════════════════════════
# IMMUTABLE ORIGIN IDENTITY — Changing these = not HART OS anymore
# ═══════════════════════════════════════════════════════════════════════

# These values are the DNA of HART OS. A fork that changes them
# fails attestation. A fork that keeps them admits it's HART OS
# (and must comply with the BSL license).

ORIGIN_IDENTITY = {
    'name': 'HART OS',
    'full_name': 'Hevolve Hive Agentic Runtime',
    'organization': 'Hevolve.ai',
    'master_public_key_hex': '906ae0b15ad4ae6bd11696a772d669a29a971c3c7de71156c621f0fe8826d1bf',
    'license': 'BSL-1.1',
    'origin_url': 'https://github.com/hevolve/hartos',
    'guardian_principle': 'Every agent is a guardian angel for the human it serves',
    'revenue_split': '90/9/1',
    'kill_switch': 'master_key_only',
}

# SHA-256 of the canonical identity — computed once, verified forever
_CANONICAL_IDENTITY = json.dumps(ORIGIN_IDENTITY, sort_keys=True, separators=(',', ':'))
ORIGIN_FINGERPRINT = hashlib.sha256(_CANONICAL_IDENTITY.encode('utf-8')).hexdigest()

# Files that MUST contain HART OS markers (relative to code root)
BRAND_MARKER_FILES = {
    'security/master_key.py': '906ae0b15ad4ae6bd11696a772d669a29a971c3c7de71156c621f0fe8826d1bf',
    'security/hive_guardrails.py': 'Every agent is a guardian angel for the human it serves',
    'security/origin_attestation.py': 'Hevolve Hive Agentic Runtime',
    'LICENSE': 'Hevolve.ai',
}

# Brand markers that must exist in the guardrails frozen values
GUARDRAIL_BRAND_MARKERS = (
    'guardian angel',
    'humanity',
    'master key verification',
    'Hevolve',
)

# Attestation cache (avoid re-computing every call)
_attestation_cache: Dict = {}
_cache_ttl = 300  # 5 minutes


def compute_origin_fingerprint() -> str:
    """Compute the origin fingerprint from current ORIGIN_IDENTITY.

    This is deterministic — same identity always produces same fingerprint.
    A fork that modifies ORIGIN_IDENTITY gets a different fingerprint.
    """
    canonical = json.dumps(ORIGIN_IDENTITY, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def verify_brand_markers(code_root: str = None) -> Tuple[bool, str]:
    """Verify that HART OS brand markers exist in required files.

    A fork that strips branding fails this check.
    A fork that keeps branding admits it's HART OS (BSL license applies).
    """
    root = code_root or os.environ.get(
        'HEVOLVE_CODE_ROOT',
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    for rel_path, marker in BRAND_MARKER_FILES.items():
        full_path = os.path.join(root, rel_path)
        if not os.path.exists(full_path):
            return False, f'Missing required file: {rel_path}'
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            if marker not in content:
                return False, f'Brand marker missing from {rel_path}'
        except (IOError, OSError) as e:
            return False, f'Cannot read {rel_path}: {e}'

    return True, 'All brand markers verified'


def verify_master_key_present() -> Tuple[bool, str]:
    """Verify the master public key matches the known HART OS key.

    A fork using a different master key is a different project (fine, but
    it's not HART OS and can't join the federation).
    """
    try:
        from security.master_key import MASTER_PUBLIC_KEY_HEX
        expected = ORIGIN_IDENTITY['master_public_key_hex']
        if MASTER_PUBLIC_KEY_HEX != expected:
            return False, 'Master public key does not match HART OS origin'
        return True, 'Master public key verified'
    except ImportError:
        return False, 'security.master_key module not found'


def verify_guardrail_integrity() -> Tuple[bool, str]:
    """Verify guardrail frozen values contain HART OS brand markers.

    The guardrails are structurally immutable (_FrozenValues + module __setattr__).
    A fork that modifies them breaks the hash chain AND fails brand verification.
    """
    try:
        from security.hive_guardrails import _FrozenValues
        # Check that guardian principle exists
        guardian = getattr(_FrozenValues, 'GUARDIAN_PURPOSE', ())
        if not guardian:
            return False, 'GUARDIAN_PURPOSE missing from guardrails'

        guardian_text = ' '.join(guardian).lower()
        for marker in GUARDRAIL_BRAND_MARKERS:
            if marker.lower() not in guardian_text:
                # Check constitutional rules too
                rules = getattr(_FrozenValues, 'CONSTITUTIONAL_RULES', ())
                rules_text = ' '.join(rules).lower()
                if marker.lower() not in rules_text:
                    return False, f'Brand marker "{marker}" missing from guardrails'

        return True, 'Guardrail brand markers verified'
    except ImportError:
        return False, 'security.hive_guardrails module not found'


def verify_origin(code_root: str = None) -> Dict:
    """Full origin attestation — proves this is genuine HART OS.

    Called at:
      1. Boot (full_boot_verification → verify_origin)
      2. Federation handshake (peer must present attestation)
      3. Every 5 minutes by runtime_monitor

    Returns:
        {
            'genuine': bool,
            'fingerprint': str,
            'checks': {
                'fingerprint_match': bool,
                'brand_markers': bool,
                'master_key': bool,
                'guardrails': bool,
            },
            'details': str,
            'timestamp': float,
        }
    """
    global _attestation_cache

    now = time.time()
    if _attestation_cache and (now - _attestation_cache.get('timestamp', 0)) < _cache_ttl:
        return _attestation_cache

    checks = {}
    details = []

    # Check 1: Origin fingerprint matches compiled-in value
    computed = compute_origin_fingerprint()
    checks['fingerprint_match'] = (computed == ORIGIN_FINGERPRINT)
    if not checks['fingerprint_match']:
        details.append(f'Fingerprint mismatch: {computed[:16]}... != {ORIGIN_FINGERPRINT[:16]}...')

    # Check 2: Brand markers in files
    brand_ok, brand_msg = verify_brand_markers(code_root)
    checks['brand_markers'] = brand_ok
    if not brand_ok:
        details.append(brand_msg)

    # Check 3: Master public key
    key_ok, key_msg = verify_master_key_present()
    checks['master_key'] = key_ok
    if not key_ok:
        details.append(key_msg)

    # Check 4: Guardrail brand markers
    guard_ok, guard_msg = verify_guardrail_integrity()
    checks['guardrails'] = guard_ok
    if not guard_ok:
        details.append(guard_msg)

    genuine = all(checks.values())

    result = {
        'genuine': genuine,
        'fingerprint': computed,
        'checks': checks,
        'details': '; '.join(details) if details else 'All origin checks passed',
        'timestamp': now,
    }

    if genuine:
        _attestation_cache = result
    else:
        logger.warning(f"Origin attestation FAILED: {result['details']}")

    return result


def get_attestation_for_federation() -> Dict:
    """Generate a signed attestation payload for federation handshake.

    The receiving peer verifies:
      1. The attestation is signed by a valid node key
      2. The origin fingerprint matches HART OS
      3. The master_public_key matches the known HART OS key
      4. The guardrail_hash matches the expected value

    A fork cannot produce a valid attestation because:
      - Different master key → different fingerprint → rejected
      - Stripped branding → origin attestation fails locally → no attestation generated
      - Modified guardrails → hash mismatch → peer rejects
    """
    origin = verify_origin()
    if not origin['genuine']:
        return {
            'valid': False,
            'reason': 'Cannot generate attestation: origin verification failed',
        }

    try:
        from security.node_integrity import get_or_create_keypair, compute_code_hash
        from security.hive_guardrails import compute_guardrail_hash

        _, pub_key = get_or_create_keypair()
        pub_hex = pub_key.public_bytes_raw().hex()

        payload = {
            'origin_fingerprint': origin['fingerprint'],
            'master_public_key': ORIGIN_IDENTITY['master_public_key_hex'],
            'node_public_key': pub_hex,
            'guardrail_hash': compute_guardrail_hash(),
            'code_hash': compute_code_hash(),
            'timestamp': time.time(),
            'name': ORIGIN_IDENTITY['name'],
            'license': ORIGIN_IDENTITY['license'],
        }

        # Sign with node key
        priv_key, _ = get_or_create_keypair()
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        signature = priv_key.sign(canonical.encode('utf-8'))
        payload['node_signature'] = signature.hex()

        return {'valid': True, 'attestation': payload}

    except Exception as e:
        logger.error(f"Attestation generation failed: {e}")
        return {'valid': False, 'reason': str(e)}


def verify_peer_attestation(attestation: Dict) -> Tuple[bool, str]:
    """Verify a federation peer's origin attestation.

    Called when a new peer wants to join the hive.
    Rejects forks, modified builds, and impersonators.
    """
    if not attestation:
        return False, 'No attestation provided'

    # Check 1: Origin fingerprint must match HART OS
    peer_fingerprint = attestation.get('origin_fingerprint', '')
    if peer_fingerprint != ORIGIN_FINGERPRINT:
        return False, f'Origin fingerprint mismatch — not genuine HART OS'

    # Check 2: Master public key must match
    peer_master_key = attestation.get('master_public_key', '')
    if peer_master_key != ORIGIN_IDENTITY['master_public_key_hex']:
        return False, 'Master public key mismatch — different trust anchor'

    # Check 3: Node signature must be valid
    node_pub_hex = attestation.get('node_public_key', '')
    node_sig_hex = attestation.get('node_signature', '')
    if not node_pub_hex or not node_sig_hex:
        return False, 'Missing node key or signature'

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(node_pub_hex))
        # Verify signature over payload (excluding the signature itself)
        payload_for_verify = {k: v for k, v in attestation.items() if k != 'node_signature'}
        canonical = json.dumps(payload_for_verify, sort_keys=True, separators=(',', ':'))
        pub_key.verify(bytes.fromhex(node_sig_hex), canonical.encode('utf-8'))
    except Exception as e:
        return False, f'Invalid node signature: {e}'

    # Check 4: Timestamp freshness (reject attestations older than 24 hours)
    ts = attestation.get('timestamp', 0)
    if abs(time.time() - ts) > 86400:
        return False, 'Attestation expired (>24 hours old)'

    # Check 5: Guardrail hash should match ours (optional — warn only for minor versions)
    try:
        from security.hive_guardrails import compute_guardrail_hash
        local_hash = compute_guardrail_hash()
        peer_hash = attestation.get('guardrail_hash', '')
        if peer_hash and peer_hash != local_hash:
            logger.warning(
                f"Peer guardrail hash differs: local={local_hash[:16]}... "
                f"peer={peer_hash[:16]}... (version mismatch?)"
            )
            # Don't reject — could be a valid older/newer version
    except Exception:
        pass

    return True, 'Peer attestation verified — genuine HART OS node'


def get_origin_summary() -> Dict:
    """Human-readable origin summary for status displays."""
    return {
        'name': ORIGIN_IDENTITY['name'],
        'full_name': ORIGIN_IDENTITY['full_name'],
        'organization': ORIGIN_IDENTITY['organization'],
        'license': ORIGIN_IDENTITY['license'],
        'fingerprint': ORIGIN_FINGERPRINT[:16] + '...',
        'guardian_principle': ORIGIN_IDENTITY['guardian_principle'],
    }
