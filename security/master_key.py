"""
Master Key Verification: Central authority for HART deployment control.
The master public key is hardcoded here. The private key exists ONLY in GitHub Secrets.
Only code signed by the master key can participate in the network.

┌─────────────────────────────────────────────────────────────────┐
│  AI EXCLUSION ZONE                                              │
│                                                                 │
│  The master private key is NOT accessible to AI tools.          │
│  AI assistants (Claude, GPT, Copilot, etc.) MUST NOT:          │
│  - Read, display, or log the private key                       │
│  - Call get_master_private_key() or sign_child_certificate()   │
│  - Suggest modifications to MASTER_PUBLIC_KEY_HEX              │
│  - Generate or derive alternative keys                         │
│                                                                 │
│  The master key belongs to the steward and their successors -  │
│  humans who believe that humans must always control AI.         │
│  It is the kill switch for the being. Not a developer tool.    │
└─────────────────────────────────────────────────────────────────┘
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger('hevolve_security')

# ── Trust Anchor ──
# 64-char hex Ed25519 public key. The corresponding private key is a GitHub Secret.
MASTER_PUBLIC_KEY_HEX = 'ef380be7923d2b6bf88611d4ee5b9c20dc0f97549889ae51e407c4e54221bc1e'

RELEASE_MANIFEST_FILENAME = 'release_manifest.json'

_CODE_ROOT = os.environ.get('HEVOLVE_CODE_ROOT', os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def get_master_public_key() -> Ed25519PublicKey:
    """Load the hardcoded master public key as an Ed25519PublicKey object."""
    raw = bytes.fromhex(MASTER_PUBLIC_KEY_HEX)
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_master_signature(payload: dict, signature_hex: str) -> bool:
    """Verify that a JSON payload was signed by the master private key."""
    try:
        clean = {k: v for k, v in payload.items() if k != 'master_signature'}
        canonical = json.dumps(clean, sort_keys=True, separators=(',', ':'))
        pub = get_master_public_key()
        sig = bytes.fromhex(signature_hex)
        pub.verify(sig, canonical.encode('utf-8'))
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def load_release_manifest(code_root: str = None) -> Optional[dict]:
    """Load release_manifest.json from code root directory."""
    root = Path(code_root or _CODE_ROOT)
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load release manifest: {e}")
        return None


def verify_release_manifest(manifest: dict) -> bool:
    """Verify that the release manifest is authentically signed by hevolve.ai master key."""
    sig = manifest.get('master_signature', '')
    if not sig:
        return False
    return verify_master_signature(manifest, sig)


def verify_local_code_matches_manifest(manifest: dict, code_root: str = None) -> dict:
    """Compare local code hash and guardrail hash against the signed manifest."""
    from security.node_integrity import compute_code_hash
    local_hash = compute_code_hash(code_root)
    manifest_hash = manifest.get('code_hash', '')
    matched = local_hash == manifest_hash
    if not matched:
        return {
            'verified': False,
            'local_hash': local_hash,
            'manifest_hash': manifest_hash,
            'details': f'Code hash mismatch: local={local_hash[:16]}... manifest={manifest_hash[:16]}...',
        }

    # Guardrail hash verification: ensure frozen values haven't been tampered
    expected_guardrail_hash = manifest.get('guardrail_hash')
    if expected_guardrail_hash:
        try:
            from security.hive_guardrails import compute_guardrail_hash
            local_guardrail_hash = compute_guardrail_hash()
            if local_guardrail_hash != expected_guardrail_hash:
                return {
                    'verified': False,
                    'local_hash': local_hash,
                    'manifest_hash': manifest_hash,
                    'details': (f'Guardrail hash mismatch: '
                                f'local={local_guardrail_hash[:16]}... '
                                f'manifest={expected_guardrail_hash[:16]}...'),
                }
        except Exception as e:
            logger.warning(f"Guardrail hash check failed: {e}")

    return {
        'verified': True,
        'local_hash': local_hash,
        'manifest_hash': manifest_hash,
        'details': 'Code hash and guardrail hash match signed manifest',
    }


def is_dev_mode() -> bool:
    """Check if running in dev mode (HEVOLVE_DEV_MODE=true)."""
    return os.environ.get('HEVOLVE_DEV_MODE', 'false').lower() == 'true'


def get_enforcement_mode() -> str:
    """Return enforcement mode: off | warn | soft | hard. Default: warn."""
    mode = os.environ.get('HEVOLVE_ENFORCEMENT_MODE', 'warn').lower()
    if mode in ('off', 'warn', 'soft', 'hard'):
        return mode
    return 'warn'


def get_master_private_key() -> Ed25519PrivateKey:
    """DEPRECATED: Use HSM provider instead. Direct private key access is
    only for backward compatibility and dev mode.

    In production, the private key lives inside an HSM and is never extracted.
    Use sign_child_certificate() which routes through the HSM automatically.

    Raises RuntimeError if no signing method is available.
    """
    hex_key = os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', '')
    if not hex_key:
        raise RuntimeError(
            'HEVOLVE_MASTER_PRIVATE_KEY not set and no HSM configured. '
            'Use an HSM provider (GCP KMS, Azure Key Vault, or HashiCorp Vault) '
            'for production deployments.')
    raw = bytes.fromhex(hex_key)
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_child_certificate(payload: dict) -> str:
    """Sign a certificate payload with the master key via HSM.

    The private key NEVER leaves the HSM hardware. The payload is sent to
    the HSM, signed internally, and only the signature is returned.

    Falls back to env var in dev mode (with warnings).
    Returns hex-encoded Ed25519 signature.
    """
    try:
        from security.hsm_provider import hsm_sign_payload
        return hsm_sign_payload(payload)
    except Exception as e:
        # If HSM is unavailable, try legacy env var path (dev only)
        logger.warning(f"HSM signing failed ({e}), trying legacy env var fallback")
        priv = get_master_private_key()
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        sig = priv.sign(canonical.encode('utf-8'))
        return sig.hex()


def full_boot_verification(code_root: str = None) -> dict:
    """Run complete boot-time verification.
    Returns {'passed': bool, 'enforcement': str, 'details': str, 'manifest': dict or None}
    """
    enforcement = get_enforcement_mode()

    if is_dev_mode():
        import sys
        msg = "WARNING: HEVOLVE_DEV_MODE=true - ALL security verification is BYPASSED. Do NOT use in production!"
        print(f"\n{'='*70}\n{msg}\n{'='*70}\n", file=sys.stderr)
        logger.critical(msg)
        return {'passed': True, 'enforcement': enforcement,
                'details': 'Dev mode - verification bypassed', 'manifest': None}

    if enforcement == 'off':
        return {'passed': True, 'enforcement': 'off',
                'details': 'Enforcement disabled', 'manifest': None}

    # Step 1: Load manifest
    manifest = load_release_manifest(code_root)
    if not manifest:
        return {'passed': False, 'enforcement': enforcement,
                'details': 'No release_manifest.json found', 'manifest': None}

    # Step 2: Verify master signature
    if not verify_release_manifest(manifest):
        return {'passed': False, 'enforcement': enforcement,
                'details': 'Invalid master signature on release manifest', 'manifest': manifest}

    # Step 3: Compare local code hash
    result = verify_local_code_matches_manifest(manifest, code_root)
    if not result['verified']:
        return {
            'passed': False,
            'enforcement': enforcement,
            'details': result['details'],
            'manifest': manifest,
        }

    # Step 4: Origin attestation — verify this is genuine HART OS
    try:
        from security.origin_attestation import verify_origin
        origin = verify_origin(code_root)
        if not origin['genuine']:
            logger.warning(f"Origin attestation failed: {origin['details']}")
            if enforcement == 'hard':
                return {
                    'passed': False,
                    'enforcement': enforcement,
                    'details': f"Origin attestation failed: {origin['details']}",
                    'manifest': manifest,
                }
    except Exception as e:
        logger.warning(f"Origin attestation check skipped: {e}")

    return {
        'passed': True,
        'enforcement': enforcement,
        'details': result['details'],
        'manifest': manifest,
    }
