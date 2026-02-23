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
import secrets
import socket
import threading
from pathlib import Path
from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger('hevolve_security')

_DEFAULT_CERT_PATH = os.path.join(
    os.environ.get('HEVOLVE_KEY_DIR', 'agent_data'), 'node_certificate.json')

# Trusted Hevolve infrastructure domains.  HARDCODED — not configurable via
# env var because this repo is open-sourced and env vars are trivially spoofed.
# Domain match alone does NOT grant regional authorization; it grants
# PROVISIONAL status that central must confirm via challenge-response.
_TRUSTED_DOMAINS = ('hevolve.ai', 'hertzai.com')

# Challenge-response protocol constants (used by DomainChallengeVerifier
# and _generate_domain_nonce).
_CHALLENGE_TTL_SECONDS = 60
_CHALLENGE_NONCE_BYTES = 32
_MAX_CHALLENGES_PER_FQDN_PER_HOUR = 5
_PROVISIONAL_CERT_VALIDITY_DAYS = 7


def _detect_node_domain() -> str:
    """Detect the FQDN of this node via OS-level resolution only.

    No env var override — that would be trivially spoofable once the repo
    is public.  Uses socket.getfqdn() which queries the machine's actual
    DNS configuration.

    Returns lowercase FQDN string, or empty string if undetectable.
    """
    try:
        fqdn = socket.getfqdn().lower()
        logger.debug(f"Node domain from socket.getfqdn(): {fqdn}")
        return fqdn
    except Exception as e:
        logger.warning(f"Failed to detect node FQDN: {e}")
        return ''


def _is_trusted_domain(fqdn: str) -> bool:
    """Check if an FQDN belongs to a trusted Hevolve infrastructure domain.

    Matches if the FQDN ends with any entry in _TRUSTED_DOMAINS.
    For example, 'regional-us.hevolve.ai' matches 'hevolve.ai'.

    Rejects empty strings, bare names without dots, and domains that
    merely contain the suffix (e.g. 'malicioushevolve.ai').
    """
    if not fqdn or '.' not in fqdn:
        return False
    for domain in _TRUSTED_DOMAINS:
        if fqdn == domain:
            return True
        if fqdn.endswith('.' + domain):
            return True
    return False


def _generate_domain_nonce() -> str:
    """Generate a cryptographic nonce for domain challenge-response verification.

    Uses 32 bytes (256 bits) of cryptographic randomness, consistent with
    the DomainChallengeVerifier challenge nonce size.
    """
    return secrets.token_hex(_CHALLENGE_NONCE_BYTES)


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

    # Check expiry (expires_at is mandatory - perpetual certs are rejected)
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
    - central: Must have master private key (HSM or HEVOLVE_MASTER_PRIVATE_KEY).
      Public key must match MASTER_PUBLIC_KEY_HEX.
    - regional: Tries two paths in order:
        1. Certificate (node_certificate.json) signed by master key → FULL auth.
        2. Trusted domain (socket.getfqdn() matches hardcoded *.hevolve.ai /
           *.hertzai.com) → PROVISIONAL auth only. Provisional nodes can
           operate but central must verify via challenge-response and
           auto-issue a short-lived certificate. Domain list is hardcoded
           (not env-var configurable) because the repo is open-sourced.
    - local/flat: Always authorized.

    Returns: {'authorized': bool, 'tier': str, 'provisional': bool, 'details': str}
    """
    tier = get_node_tier()

    if tier in ('local', 'flat'):
        return {'authorized': True, 'tier': tier,
                'details': 'Local/flat tier - no credentials required'}

    if tier == 'central':
        # Check HSM provider first (production path)
        try:
            from security.hsm_provider import get_hsm_provider
            provider = get_hsm_provider()
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            hsm_pub = provider.get_public_key_hex()
            if hsm_pub == MASTER_PUBLIC_KEY_HEX:
                return {'authorized': True, 'tier': tier,
                        'details': f'Central tier authorized - HSM ({provider.get_provider_name()})'}
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
                    'details': 'Central tier authorized - env var fallback (use HSM in production)'}
        except (ValueError, Exception) as e:
            return {'authorized': False, 'tier': tier,
                    'details': f'Invalid master private key: {e}'}

    if tier == 'regional':
        # Path 1: Certificate-based authorization (FULL — preferred)
        cert = load_node_certificate()
        if cert:
            chain_result = verify_certificate_chain(cert)
            if chain_result['valid']:
                if cert.get('tier') != 'regional':
                    return {'authorized': False, 'tier': tier,
                            'details': f'Certificate tier mismatch: cert says '
                                       f'{cert.get("tier")}, node claims regional'}
                return {'authorized': True, 'tier': tier,
                        'provisional': False,
                        'details': f'Regional tier authorized via certificate '
                                   f'({chain_result["path"]})'}
            else:
                logger.warning(
                    f'Regional certificate invalid ({chain_result["details"]}), '
                    f'trying domain-based provisional authorization')

        # Path 2: Trusted domain — PROVISIONAL only
        #
        # Domain detection (socket.getfqdn) is not cryptographic proof.
        # An attacker who controls their own DNS could spoof this.
        # Therefore domain match grants PROVISIONAL status:
        #   - Node can join the gossip network
        #   - Node can host agents locally
        #   - Node CANNOT sign certificates or act as authority
        #   - Central will issue a challenge-response nonce to verify the
        #     node is actually reachable at the claimed FQDN, then auto-issue
        #     a short-lived certificate if verification passes.
        fqdn = _detect_node_domain()
        if fqdn and _is_trusted_domain(fqdn):
            nonce = _generate_domain_nonce()
            logger.info(
                f'PROVISIONAL REGIONAL AUTH: {fqdn} matches trusted domain. '
                f'Node authorized provisionally pending central verification. '
                f'Challenge nonce: {nonce[:8]}...')
            return {'authorized': True, 'tier': tier,
                    'provisional': True,
                    'fqdn': fqdn,
                    'challenge_nonce': nonce,
                    'details': f'Regional tier PROVISIONAL via trusted domain '
                               f'({fqdn}) — pending central challenge-response'}

        # Neither path succeeded
        if cert:
            return {'authorized': False, 'tier': tier,
                    'details': f'Certificate invalid: {chain_result["details"]}; '
                               f'domain "{fqdn or "(undetectable)"}" not in '
                               f'trusted list'}
        return {'authorized': False, 'tier': tier,
                'details': f'Regional tier requires a signed certificate or '
                           f'trusted domain FQDN; detected domain: '
                           f'"{fqdn or "(undetectable)"}"'}

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


# =========================================================================
# Domain Challenge-Response Verifier
# =========================================================================
#
# 4-step handshake for promoting provisional regional nodes to full status:
#
#   1. Node  -> Central:  REGISTER {fqdn, public_key_hex, tier_claim}
#   2. Central -> Node:   CHALLENGE (nonce delivered to http://{fqdn}:{port}/.well-known/hart-challenge)
#   3. Node  -> Central:  CHALLENGE_RESPONSE {fqdn, public_key_hex, nonce, signature_hex}
#   4. Central -> Node:   VERIFIED (short-lived certificate) or REJECTED
#
# Security properties:
#   - Nonce is 32 bytes of cryptographic randomness (secrets.token_bytes).
#   - Challenge is single-use: deleted after verification attempt or on expiry.
#   - 60-second TTL prevents replay of stale challenges.
#   - Rate limit of 5 challenges per FQDN per hour mitigates DoS / probing.
#   - HTTP callback to the FQDN proves the node controls that DNS name.
#   - Ed25519 signature binds the nonce to the node's keypair.
#   - Issued certificates are short-lived (7 days) forcing periodic re-verification.

class DomainChallengeVerifier:
    """Thread-safe challenge-response verifier for provisional regional nodes.

    Instantiated once on the central node.  All public methods are safe to call
    from concurrent Flask request threads.
    """

    def __init__(self):
        # {nonce_hex: {fqdn, public_key_hex, nonce_hex, created_at, expires_at}}
        self._pending: Dict[str, dict] = {}
        # {fqdn: [timestamp, ...]} — timestamps of recent challenge creations
        self._rate_log: Dict[str, list] = {}
        self._lock = threading.Lock()

    # -----------------------------------------------------------------
    # Step 1+2: Central creates a challenge for a registering node
    # -----------------------------------------------------------------

    def create_challenge(
        self,
        fqdn: str,
        public_key_hex: str,
    ) -> Tuple[bool, dict]:
        """Create a challenge nonce for a provisional regional node.

        Called when a node sends a REGISTER request claiming regional tier.

        Args:
            fqdn: The fully qualified domain name the node claims.
            public_key_hex: The node's Ed25519 public key in hex.

        Returns:
            (success, result_dict).  On success result_dict contains the
            nonce_hex that must be delivered to the node.  On failure it
            contains an 'error' key with a human-readable reason.
        """
        # --- Validate FQDN against trusted domains ---
        if not _is_trusted_domain(fqdn):
            logger.warning(
                f"CHALLENGE REJECTED: FQDN '{fqdn}' is not in trusted domains")
            return False, {
                'error': f'FQDN "{fqdn}" is not a trusted Hevolve domain',
                'fqdn': fqdn,
            }

        # --- Validate public key format (must be 32-byte Ed25519 key) ---
        try:
            key_bytes = bytes.fromhex(public_key_hex)
            if len(key_bytes) != 32:
                raise ValueError(f'Expected 32 bytes, got {len(key_bytes)}')
            Ed25519PublicKey.from_public_bytes(key_bytes)
        except (ValueError, Exception) as e:
            logger.warning(
                f"CHALLENGE REJECTED: invalid public key from {fqdn}: {e}")
            return False, {
                'error': f'Invalid Ed25519 public key: {e}',
                'fqdn': fqdn,
            }

        now = datetime.now(timezone.utc)

        with self._lock:
            # --- Rate limit ---
            self._prune_rate_log(fqdn, now)
            recent_count = len(self._rate_log.get(fqdn, []))
            if recent_count >= _MAX_CHALLENGES_PER_FQDN_PER_HOUR:
                logger.warning(
                    f"CHALLENGE RATE LIMITED: {fqdn} has {recent_count} "
                    f"challenges in the last hour (max {_MAX_CHALLENGES_PER_FQDN_PER_HOUR})")
                return False, {
                    'error': 'Rate limit exceeded: too many challenge requests',
                    'fqdn': fqdn,
                    'retry_after_seconds': 3600,
                }

            # --- Purge any existing expired challenges ---
            self._purge_expired(now)

            # --- Generate cryptographic nonce ---
            nonce_bytes = secrets.token_bytes(_CHALLENGE_NONCE_BYTES)
            nonce_hex = nonce_bytes.hex()

            expires_at = now + timedelta(seconds=_CHALLENGE_TTL_SECONDS)

            challenge_record = {
                'fqdn': fqdn,
                'public_key_hex': public_key_hex,
                'nonce_hex': nonce_hex,
                'created_at': now.isoformat(),
                'expires_at': expires_at.isoformat(),
            }
            self._pending[nonce_hex] = challenge_record

            # Record in rate log
            self._rate_log.setdefault(fqdn, []).append(now)

        logger.info(
            f"CHALLENGE CREATED: fqdn={fqdn} nonce={nonce_hex[:16]}... "
            f"expires={expires_at.isoformat()} "
            f"pubkey={public_key_hex[:16]}...")

        return True, {
            'nonce_hex': nonce_hex,
            'fqdn': fqdn,
            'expires_at': expires_at.isoformat(),
        }

    # -----------------------------------------------------------------
    # Step 3+4: Central verifies the signed challenge response
    # -----------------------------------------------------------------

    def verify_response(
        self,
        fqdn: str,
        public_key_hex: str,
        nonce_hex: str,
        signature_hex: str,
    ) -> Tuple[bool, dict]:
        """Verify a challenge response from a provisional regional node.

        The node must sign the raw nonce bytes with its Ed25519 private key.
        The nonce is single-use: consumed (deleted) regardless of outcome.

        Args:
            fqdn: The FQDN the node claims.
            public_key_hex: The node's Ed25519 public key in hex.
            nonce_hex: The nonce that was issued in the challenge.
            signature_hex: Hex-encoded Ed25519 signature of the nonce bytes.

        Returns:
            (success, result_dict).  On failure, result_dict has 'error'.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            # --- Look up and consume the challenge (single-use) ---
            challenge = self._pending.pop(nonce_hex, None)

        if challenge is None:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: unknown or already-consumed "
                f"nonce from {fqdn} (nonce={nonce_hex[:16]}...)")
            return False, {
                'error': 'Unknown or already-consumed challenge nonce',
                'fqdn': fqdn,
            }

        # --- Check expiry ---
        expires_at = datetime.fromisoformat(challenge['expires_at'])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: nonce expired for {fqdn} "
                f"(expired {expires_at.isoformat()}, now {now.isoformat()})")
            return False, {
                'error': 'Challenge nonce has expired',
                'fqdn': fqdn,
                'expired_at': expires_at.isoformat(),
            }

        # --- Verify FQDN matches ---
        if challenge['fqdn'] != fqdn:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: FQDN mismatch "
                f"(challenge={challenge['fqdn']}, response={fqdn})")
            return False, {
                'error': 'FQDN does not match the challenge',
                'fqdn': fqdn,
            }

        # --- Verify public key matches ---
        if challenge['public_key_hex'] != public_key_hex:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: public key mismatch for {fqdn}")
            return False, {
                'error': 'Public key does not match the challenge',
                'fqdn': fqdn,
            }

        # --- Verify Ed25519 signature on the raw nonce bytes ---
        try:
            nonce_bytes = bytes.fromhex(nonce_hex)
            sig_bytes = bytes.fromhex(signature_hex)
            pub_bytes = bytes.fromhex(public_key_hex)
            pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub_key.verify(sig_bytes, nonce_bytes)
        except InvalidSignature:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: invalid signature for {fqdn} "
                f"(nonce={nonce_hex[:16]}...)")
            return False, {
                'error': 'Invalid signature: does not match public key and nonce',
                'fqdn': fqdn,
            }
        except (ValueError, Exception) as e:
            logger.warning(
                f"CHALLENGE RESPONSE REJECTED: signature verification error "
                f"for {fqdn}: {e}")
            return False, {
                'error': f'Signature verification error: {e}',
                'fqdn': fqdn,
            }

        logger.info(
            f"CHALLENGE RESPONSE VERIFIED: fqdn={fqdn} "
            f"pubkey={public_key_hex[:16]}... "
            f"nonce={nonce_hex[:16]}...")

        return True, {
            'verified': True,
            'fqdn': fqdn,
            'public_key_hex': public_key_hex,
        }

    # -----------------------------------------------------------------
    # Step 4 (continued): Issue a short-lived certificate on success
    # -----------------------------------------------------------------

    def issue_provisional_cert(
        self,
        parent_private_key: Ed25519PrivateKey,
        fqdn: str,
        public_key_hex: str,
        region_name: str = '',
    ) -> dict:
        """Issue a short-lived regional certificate after successful verification.

        Called by central after verify_response() returns success.
        The certificate is valid for 7 days, forcing periodic re-verification.

        Args:
            parent_private_key: Central's Ed25519 private key (the signing authority).
            fqdn: The verified FQDN of the regional node.
            public_key_hex: The node's verified Ed25519 public key.
            region_name: Optional region name (e.g. 'us-east-1'). Defaults to the
                FQDN's first subdomain label if not provided.

        Returns:
            A signed certificate dict suitable for save_node_certificate().
        """
        if not region_name:
            # Derive region from FQDN: 'us-east-1.hevolve.ai' -> 'us-east-1'
            parts = fqdn.split('.')
            region_name = parts[0] if len(parts) > 2 else fqdn

        node_id = f'regional-{fqdn}'

        cert = create_child_certificate(
            parent_private_key=parent_private_key,
            child_public_key_hex=public_key_hex,
            node_id=node_id,
            tier='regional',
            region_name=region_name,
            capabilities=['gossip_hub', 'agent_host'],
            validity_days=_PROVISIONAL_CERT_VALIDITY_DAYS,
        )

        logger.info(
            f"PROVISIONAL CERT ISSUED: fqdn={fqdn} node_id={node_id} "
            f"region={region_name} validity={_PROVISIONAL_CERT_VALIDITY_DAYS}d "
            f"expires={cert['expires_at']}")

        return cert

    # -----------------------------------------------------------------
    # Full handshake orchestrator (convenience for central controller)
    # -----------------------------------------------------------------

    def handle_register(
        self,
        fqdn: str,
        public_key_hex: str,
        challenge_port: int = 6777,
    ) -> Tuple[bool, dict]:
        """Handle a full REGISTER request: validate, create challenge, and
        deliver it to the node via HTTP callback.

        This is the entry point for step 1+2 of the handshake.  Central calls
        this when a node sends a registration request.

        Args:
            fqdn: The FQDN the node claims.
            public_key_hex: The node's Ed25519 public key.
            challenge_port: The port the node's Flask server listens on.

        Returns:
            (success, result_dict).  On success, result_dict includes
            'nonce_hex' and 'callback_status'.
        """
        ok, result = self.create_challenge(fqdn, public_key_hex)
        if not ok:
            return False, result

        nonce_hex = result['nonce_hex']

        # Deliver challenge to the node via HTTP GET
        callback_url = f'http://{fqdn}:{challenge_port}/.well-known/hart-challenge'
        try:
            import requests as http_requests
            resp = http_requests.get(
                callback_url,
                params={'nonce': nonce_hex},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"CHALLENGE DELIVERY FAILED: {callback_url} returned "
                    f"HTTP {resp.status_code}")
                # Clean up the pending challenge since delivery failed
                with self._lock:
                    self._pending.pop(nonce_hex, None)
                return False, {
                    'error': f'Challenge delivery failed: HTTP {resp.status_code}',
                    'fqdn': fqdn,
                    'callback_url': callback_url,
                }

            logger.info(
                f"CHALLENGE DELIVERED: {callback_url} returned HTTP 200 "
                f"(nonce={nonce_hex[:16]}...)")

            result['callback_status'] = 'delivered'
            result['callback_url'] = callback_url
            return True, result

        except Exception as e:
            logger.warning(
                f"CHALLENGE DELIVERY FAILED: could not reach {callback_url}: {e}")
            # Clean up the pending challenge since delivery failed
            with self._lock:
                self._pending.pop(nonce_hex, None)
            return False, {
                'error': f'Cannot reach node at {callback_url}: {e}',
                'fqdn': fqdn,
                'callback_url': callback_url,
            }

    def handle_challenge_response(
        self,
        fqdn: str,
        public_key_hex: str,
        nonce_hex: str,
        signature_hex: str,
        parent_private_key: Ed25519PrivateKey,
        region_name: str = '',
    ) -> Tuple[bool, dict]:
        """Handle a CHALLENGE_RESPONSE: verify signature and issue certificate.

        This is the entry point for steps 3+4 of the handshake.

        Args:
            fqdn: The FQDN the node claims.
            public_key_hex: The node's Ed25519 public key.
            nonce_hex: The nonce from the challenge.
            signature_hex: Ed25519 signature of the nonce bytes.
            parent_private_key: Central's private key for signing certs.
            region_name: Optional region name for the certificate.

        Returns:
            (success, result_dict).  On success, result_dict includes
            'certificate' containing the signed short-lived cert.
        """
        ok, result = self.verify_response(
            fqdn, public_key_hex, nonce_hex, signature_hex)
        if not ok:
            return False, result

        cert = self.issue_provisional_cert(
            parent_private_key=parent_private_key,
            fqdn=fqdn,
            public_key_hex=public_key_hex,
            region_name=region_name,
        )

        result['certificate'] = cert
        return True, result

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _purge_expired(self, now: datetime) -> int:
        """Remove expired challenges.  Caller must hold self._lock."""
        expired_keys = []
        for nonce_hex, record in self._pending.items():
            expires_at = datetime.fromisoformat(record['expires_at'])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at:
                expired_keys.append(nonce_hex)
        for key in expired_keys:
            del self._pending[key]
        if expired_keys:
            logger.debug(f"Purged {len(expired_keys)} expired challenges")
        return len(expired_keys)

    def _prune_rate_log(self, fqdn: str, now: datetime):
        """Remove rate-log entries older than 1 hour.  Caller must hold self._lock."""
        cutoff = now - timedelta(hours=1)
        if fqdn in self._rate_log:
            self._rate_log[fqdn] = [
                ts for ts in self._rate_log[fqdn] if ts > cutoff
            ]

    def get_pending_count(self) -> int:
        """Return the number of pending (not yet verified) challenges."""
        with self._lock:
            return len(self._pending)

    def get_pending_for_fqdn(self, fqdn: str) -> int:
        """Return the number of pending challenges for a specific FQDN."""
        with self._lock:
            return sum(
                1 for r in self._pending.values() if r['fqdn'] == fqdn)
