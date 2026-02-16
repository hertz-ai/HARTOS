"""
Key Delegation: Hierarchical certificate chain for 3-tier HevolveSocial network.

Central (hevolve.ai) signs certificates for Regional hosts.
Regional hosts are verified via certificate chain back to master key
AND/OR trusted-keys registry lookup (hybrid model).
Local nodes (Nunba) connect to their assigned regional host.

Certificate format:
{
    "node_id": "...",
    "public_key": "<hex>",
    "tier": "regional",
    "region_name": "us-east-1",
    "issued_at": "ISO8601",
    "expires_at": "ISO8601",
    "capabilities": ["registry", "gossip_hub", "agent_host"],
    "parent_public_key": "<hex>",
    "parent_signature": "<hex>"
}
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger('hevolve_security')

_DEFAULT_CERT_PATH = os.path.join(
    os.environ.get('HEVOLVE_KEY_DIR', 'agent_data'), 'node_certificate.json')


def get_node_tier() -> str:
    """Return node tier from env var. Default: 'flat' (backward-compatible)."""
    tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat').lower()
    if tier in ('central', 'regional', 'local', 'flat'):
        return tier
    return 'flat'


def create_child_certificate(
    parent_private_key: Ed25519PrivateKey,
    child_public_key_hex: str,
    node_id: str,
    tier: str,
    region_name: str,
    capabilities: list = None,
    validity_days: int = 365,
) -> dict:
    """Create a certificate for a child node, signed by the parent's private key.

    Used by central to certify regional hosts, or by regional to certify locals.
    """
    MAX_CERT_VALIDITY_DAYS = 365
    validity_days = min(validity_days, MAX_CERT_VALIDITY_DAYS)

    parent_pub_bytes = parent_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = datetime.now(timezone.utc)
    cert = {
        'node_id': node_id,
        'public_key': child_public_key_hex,
        'tier': tier,
        'region_name': region_name,
        'issued_at': now.isoformat(),
        'expires_at': (now + timedelta(days=validity_days)).isoformat(),
        'capabilities': capabilities or ['gossip_hub', 'agent_host'],
        'parent_public_key': parent_pub_bytes.hex(),
    }

    # Sign all fields except parent_signature
    canonical = json.dumps(cert, sort_keys=True, separators=(',', ':'))
    sig = parent_private_key.sign(canonical.encode('utf-8'))
    cert['parent_signature'] = sig.hex()
    return cert


def verify_certificate_signature(certificate: dict) -> bool:
    """Verify that a certificate's parent_signature is valid.

    Checks signature against the parent_public_key embedded in the certificate.
    """
    try:
        parent_sig = certificate.get('parent_signature', '')
        parent_pub_hex = certificate.get('parent_public_key', '')
        if not parent_sig or not parent_pub_hex:
            return False

        clean = {k: v for k, v in certificate.items() if k != 'parent_signature'}
        canonical = json.dumps(clean, sort_keys=True, separators=(',', ':'))

        pub_bytes = bytes.fromhex(parent_pub_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = bytes.fromhex(parent_sig)
        pub_key.verify(sig_bytes, canonical.encode('utf-8'))
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def verify_certificate_chain(
    certificate: dict,
    trusted_keys: dict = None,
) -> dict:
    """Verify a certificate using hybrid approach.

    Path 1 (Certificate chain): Verify parent_signature, then check if
    parent_public_key traces back to MASTER_PUBLIC_KEY_HEX.

    Path 2 (Registry lookup): Check if certificate's public_key is in
    the trusted_keys dict.

    Either path succeeding = valid.

    Returns: {'valid': bool, 'path': str, 'details': str}
    """
    node_id = certificate.get('node_id', 'unknown')
    pub_key = certificate.get('public_key', '')

    # Path 1: Certificate chain verification
    chain_valid = False
    chain_details = ''
    try:
        # Step 1: Verify signature on certificate
        if verify_certificate_signature(certificate):
            # Step 2: Check if parent_public_key is the master key
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            parent_pub = certificate.get('parent_public_key', '')
            if parent_pub == MASTER_PUBLIC_KEY_HEX:
                chain_valid = True
                chain_details = 'Certificate signed by master key'
            else:
                chain_details = 'Certificate signed by non-master key'
        else:
            chain_details = 'Invalid certificate signature'
    except Exception as e:
        chain_details = f'Chain verification error: {e}'

    # Check expiry (expires_at is mandatory — perpetual certs are rejected)
    if chain_valid:
        try:
            expires_str = certificate.get('expires_at', '')
            if not expires_str:
                chain_valid = False
                chain_details = 'Certificate missing expires_at field'
            elif expires_str:
                expires = datetime.fromisoformat(expires_str)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    chain_valid = False
                    chain_details = 'Certificate expired'
        except (ValueError, TypeError):
            chain_valid = False
            chain_details = 'Malformed certificate expiry date'

    # Path 2: Registry lookup (fallback)
    registry_valid = False
    registry_details = ''
    if trusted_keys and pub_key:
        if trusted_keys.get(node_id) == pub_key:
            registry_valid = True
            registry_details = 'Public key found in trusted registry'
        else:
            registry_details = 'Public key not in trusted registry'

    # Hybrid: either path succeeding = valid
    valid = chain_valid or registry_valid
    if valid:
        path = 'chain' if chain_valid else 'registry'
        details = chain_details if chain_valid else registry_details
    else:
        details = f'Chain: {chain_details}; Registry: {registry_details or "not checked"}'
        path = 'none'

    return {'valid': valid, 'path': path, 'details': details}


def verify_tier_authorization() -> dict:
    """Verify this node has proper credentials for its claimed tier.

    Enforcement rules:
    - central: Must have HEVOLVE_MASTER_PRIVATE_KEY env var. Its public key
      must match MASTER_PUBLIC_KEY_HEX. Without the master private key,
      a node cannot sign certificates or prove central authority.
    - regional: Must have a valid certificate (node_certificate.json) signed
      by the master key. Without this, peers will reject the node.
    - local/flat: Always authorized. Local nodes run agents on local SQLite
      without any certificate. They only need credentials to sync to cloud.

    Returns: {'authorized': bool, 'tier': str, 'details': str}
    """
    tier = get_node_tier()

    if tier in ('local', 'flat'):
        return {'authorized': True, 'tier': tier,
                'details': 'Local/flat tier — no credentials required'}

    if tier == 'central':
        # Check HSM provider first (production path)
        try:
            from security.hsm_provider import get_hsm_provider
            provider = get_hsm_provider()
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            hsm_pub = provider.get_public_key_hex()
            if hsm_pub == MASTER_PUBLIC_KEY_HEX:
                return {'authorized': True, 'tier': tier,
                        'details': f'Central tier authorized — HSM ({provider.get_provider_name()})'}
            else:
                return {'authorized': False, 'tier': tier,
                        'details': 'HSM public key does not match trust anchor'}
        except Exception:
            pass

        # Legacy fallback: check env var (dev mode)
        priv_hex = os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', '')
        if not priv_hex:
            return {'authorized': False, 'tier': tier,
                    'details': 'Central tier requires HSM or HEVOLVE_MASTER_PRIVATE_KEY'}
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
            pub_hex = priv.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ).hex()
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            if pub_hex != MASTER_PUBLIC_KEY_HEX:
                return {'authorized': False, 'tier': tier,
                        'details': 'Master private key does not match hardcoded public key'}
            return {'authorized': True, 'tier': tier,
                    'details': 'Central tier authorized — env var fallback (use HSM in production)'}
        except (ValueError, Exception) as e:
            return {'authorized': False, 'tier': tier,
                    'details': f'Invalid master private key: {e}'}

    if tier == 'regional':
        cert = load_node_certificate()
        if not cert:
            return {'authorized': False, 'tier': tier,
                    'details': 'Regional tier requires a signed certificate (node_certificate.json)'}
        chain_result = verify_certificate_chain(cert)
        if not chain_result['valid']:
            return {'authorized': False, 'tier': tier,
                    'details': f'Certificate invalid: {chain_result["details"]}'}
        if cert.get('tier') != 'regional':
            return {'authorized': False, 'tier': tier,
                    'details': f'Certificate tier mismatch: cert says {cert.get("tier")}, node claims regional'}
        return {'authorized': True, 'tier': tier,
                'details': f'Regional tier authorized via {chain_result["path"]}'}

    return {'authorized': False, 'tier': tier, 'details': f'Unknown tier: {tier}'}


def load_node_certificate(cert_path: str = None) -> Optional[dict]:
    """Load this node's certificate from disk."""
    path = Path(cert_path or os.environ.get('HEVOLVE_NODE_CERT_PATH', _DEFAULT_CERT_PATH))
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load node certificate: {e}")
        return None


def save_node_certificate(certificate: dict, cert_path: str = None):
    """Persist node certificate to disk."""
    path = Path(cert_path or os.environ.get('HEVOLVE_NODE_CERT_PATH', _DEFAULT_CERT_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(certificate, f, indent=2)
    logger.info(f"Node certificate saved to {path}")
