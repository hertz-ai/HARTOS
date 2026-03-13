"""
HSM Provider - Hardware Security Module abstraction for master key operations.

The master private key NEVER leaves the HSM. All signing happens inside
the hardware. The application sends a payload, the HSM signs it internally,
and returns only the signature. No private key bytes ever appear in memory.

Supported backends (in priority order):
  1. Google Cloud KMS       - Ed25519, FIPS 140-2 Level 3
  2. Azure Key Vault HSM    - Ed25519, FIPS 140-2 Level 3
  3. HashiCorp Vault Transit - Ed25519, self-hostable
  4. AWS CloudHSM           - via PKCS#11 (Ed25519 support varies)
  5. Env var fallback        - DEV MODE ONLY, loud warnings

┌─────────────────────────────────────────────────────────────────┐
│  AI EXCLUSION ZONE                                              │
│  AI tools MUST NOT call any signing function in this module.    │
│  See CLAUDE.md for the full exclusion policy.                   │
└─────────────────────────────────────────────────────────────────┘

Trust path:
  Application → mTLS/IAM → HSM API → Hardware signs → signature returned
                ↑
  Certificate pinned to HSM endpoint (see hsm_trust.py)
  Audit log of every signing operation
  Rate limited (max N signs per hour)
"""
import os
import json
import time
import logging
import threading
import hashlib
from abc import ABC, abstractmethod
from typing import Optional, Dict
from datetime import datetime

logger = logging.getLogger('hevolve_security')

# Rate limit: max signing operations per hour (safety against runaway code)
_MAX_SIGNS_PER_HOUR = int(os.environ.get('HART_HSM_MAX_SIGNS_PER_HOUR', '50'))


class HSMSigningError(Exception):
    """Raised when HSM signing operation fails."""
    pass


class HSMUnavailableError(Exception):
    """Raised when no HSM backend is available."""
    pass


# ═══════════════════════════════════════════════════════════════
# Abstract HSM Provider
# ═══════════════════════════════════════════════════════════════

class HSMProvider(ABC):
    """Abstract base for HSM backends. Key NEVER leaves the hardware."""

    def __init__(self):
        self._sign_count = 0
        self._sign_window_start = time.time()
        self._lock = threading.Lock()
        self._audit_log = []

    @abstractmethod
    def sign(self, payload_bytes: bytes) -> bytes:
        """Sign raw bytes inside HSM. Returns raw Ed25519 signature (64 bytes).
        The private key never leaves the hardware."""
        pass

    @abstractmethod
    def get_public_key_hex(self) -> str:
        """Return the public key hex from the HSM (for verification against trust anchor)."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this HSM backend is configured and reachable."""
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return human-readable provider name for audit logs."""
        pass

    def sign_json_payload(self, payload: dict) -> str:
        """Sign a JSON payload canonically. Returns hex signature.
        Enforces rate limiting and audit logging."""
        self._enforce_rate_limit()

        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        payload_bytes = canonical.encode('utf-8')

        sig_bytes = self.sign(payload_bytes)
        sig_hex = sig_bytes.hex()

        self._audit_sign(payload, sig_hex)
        return sig_hex

    def _enforce_rate_limit(self):
        """Rate limit signing operations. Safety against runaway code."""
        with self._lock:
            now = time.time()
            if now - self._sign_window_start > 3600:
                self._sign_count = 0
                self._sign_window_start = now
            self._sign_count += 1
            if self._sign_count > _MAX_SIGNS_PER_HOUR:
                raise HSMSigningError(
                    f'HSM rate limit exceeded: {self._sign_count} signs in current hour '
                    f'(max {_MAX_SIGNS_PER_HOUR}). This may indicate runaway code.')

    def _audit_sign(self, payload: dict, sig_hex: str):
        """Log every signing operation for audit trail."""
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'provider': self.get_provider_name(),
            'payload_hash': hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16],
            'signature_prefix': sig_hex[:16],
            'sign_count': self._sign_count,
        }
        with self._lock:
            self._audit_log.append(entry)
            # Keep last 1000 entries
            if len(self._audit_log) > 1000:
                self._audit_log = self._audit_log[-500:]
        logger.info(f"HSM sign [{entry['provider']}]: "
                     f"payload_hash={entry['payload_hash']}... "
                     f"count={entry['sign_count']}")

    def get_audit_log(self) -> list:
        """Return recent signing audit entries."""
        with self._lock:
            return list(self._audit_log)


# ═══════════════════════════════════════════════════════════════
# Google Cloud KMS Provider
# ═══════════════════════════════════════════════════════════════

class GoogleCloudKMSProvider(HSMProvider):
    """Google Cloud KMS with Ed25519 key (FIPS 140-2 Level 3 HSM backend).

    Required env vars:
      HART_GCP_KMS_KEY_PATH: projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}/cryptoKeyVersions/{version}
      GOOGLE_APPLICATION_CREDENTIALS: path to service account JSON (or use workload identity)
    """

    def __init__(self):
        super().__init__()
        self._key_path = os.environ.get('HART_GCP_KMS_KEY_PATH', '')
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import kms  # type: ignore
            self._client = kms.KeyManagementServiceClient()
        return self._client

    def sign(self, payload_bytes: bytes) -> bytes:
        from google.cloud.kms import CryptoKeyVersion  # type: ignore
        client = self._get_client()

        # CRC32C integrity check
        import struct
        import binascii
        crc32c = binascii.crc32(payload_bytes) & 0xffffffff

        response = client.asymmetric_sign(
            request={
                'name': self._key_path,
                'data': payload_bytes,
                'data_crc32c': {'value': crc32c},
            }
        )

        # Verify response integrity
        if not response.verified_data_crc32c:
            raise HSMSigningError('GCP KMS: data integrity check failed')

        return response.signature

    def get_public_key_hex(self) -> str:
        client = self._get_client()
        pub_key = client.get_public_key(request={'name': self._key_path})
        # Parse PEM to get raw Ed25519 public key bytes
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        key = load_pem_public_key(pub_key.pem.encode())
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return raw.hex()

    def is_available(self) -> bool:
        if not self._key_path:
            return False
        try:
            self._get_client()
            return True
        except Exception:
            return False

    def get_provider_name(self) -> str:
        return 'google_cloud_kms'


# ═══════════════════════════════════════════════════════════════
# Azure Key Vault HSM Provider
# ═══════════════════════════════════════════════════════════════

class AzureKeyVaultProvider(HSMProvider):
    """Azure Key Vault with Managed HSM (FIPS 140-2 Level 3).

    Required env vars:
      HART_AZURE_VAULT_URL:  https://{vault-name}.vault.azure.net
      HART_AZURE_KEY_NAME:   name of the Ed25519 key in the vault
      HART_AZURE_KEY_VERSION: (optional) specific version
      Authentication: DefaultAzureCredential (managed identity, CLI, env vars)
    """

    def __init__(self):
        super().__init__()
        self._vault_url = os.environ.get('HART_AZURE_VAULT_URL', '')
        self._key_name = os.environ.get('HART_AZURE_KEY_NAME', '')
        self._key_version = os.environ.get('HART_AZURE_KEY_VERSION', '')
        self._client = None
        self._crypto_client = None

    def _get_clients(self):
        if self._client is None:
            from azure.identity import DefaultAzureCredential  # type: ignore
            from azure.keyvault.keys import KeyClient  # type: ignore
            from azure.keyvault.keys.crypto import CryptographyClient, SignatureAlgorithm  # type: ignore
            credential = DefaultAzureCredential()
            self._client = KeyClient(vault_url=self._vault_url, credential=credential)
            key = self._client.get_key(self._key_name, self._key_version or None)
            self._crypto_client = CryptographyClient(key, credential=credential)
        return self._client, self._crypto_client

    def sign(self, payload_bytes: bytes) -> bytes:
        from azure.keyvault.keys.crypto import SignatureAlgorithm  # type: ignore
        _, crypto = self._get_clients()
        # Ed25519 signs raw bytes (no pre-hashing needed)
        result = crypto.sign(SignatureAlgorithm.eddsa, payload_bytes)
        return result.signature

    def get_public_key_hex(self) -> str:
        client, _ = self._get_clients()
        key = client.get_key(self._key_name, self._key_version or None)
        # Azure returns JWK; extract raw Ed25519 public key
        import base64
        x = key.key.x  # Raw public key bytes (base64url-encoded in JWK)
        if isinstance(x, str):
            x = base64.urlsafe_b64decode(x + '==')
        return x.hex()

    def is_available(self) -> bool:
        if not self._vault_url or not self._key_name:
            return False
        try:
            self._get_clients()
            return True
        except Exception:
            return False

    def get_provider_name(self) -> str:
        return 'azure_key_vault'


# ═══════════════════════════════════════════════════════════════
# HashiCorp Vault Transit Provider
# ═══════════════════════════════════════════════════════════════

class VaultTransitProvider(HSMProvider):
    """HashiCorp Vault Transit secrets engine with Ed25519.

    Self-hostable. Can back onto physical HSMs (PKCS#11 seal).

    Required env vars:
      HART_VAULT_ADDR:       https://vault.example.com:8200
      HART_VAULT_TOKEN:      auth token (or use AppRole/K8s auth)
      HART_VAULT_KEY_NAME:   name of the transit key (type: ed25519)
      HART_VAULT_MOUNT:      transit mount path (default: transit)
      HART_VAULT_CA_CERT:    (optional) CA cert for TLS verification
    """

    def __init__(self):
        super().__init__()
        self._addr = os.environ.get('HART_VAULT_ADDR', '')
        self._token = os.environ.get('HART_VAULT_TOKEN', '')
        self._key_name = os.environ.get('HART_VAULT_KEY_NAME', 'hart-master')
        self._mount = os.environ.get('HART_VAULT_MOUNT', 'transit')
        self._ca_cert = os.environ.get('HART_VAULT_CA_CERT', '')
        self._client = None

    def _get_client(self):
        if self._client is None:
            import hvac  # type: ignore
            kwargs = {'url': self._addr, 'token': self._token}
            if self._ca_cert:
                kwargs['verify'] = self._ca_cert
            self._client = hvac.Client(**kwargs)
        return self._client

    def sign(self, payload_bytes: bytes) -> bytes:
        import base64
        client = self._get_client()

        # Vault Transit expects base64-encoded input
        b64_input = base64.b64encode(payload_bytes).decode()

        response = client.secrets.transit.sign_data(
            name=self._key_name,
            hash_input=b64_input,
            marshaling_algorithm='jws',
            mount_point=self._mount,
        )

        # Response signature is vault:v1:base64signature
        sig_str = response['data']['signature']
        # Strip vault:vN: prefix
        sig_b64 = sig_str.split(':')[-1]
        return base64.b64decode(sig_b64)

    def get_public_key_hex(self) -> str:
        import base64
        client = self._get_client()
        response = client.secrets.transit.read_key(
            name=self._key_name,
            mount_point=self._mount,
        )
        # Get latest version's public key
        keys = response['data']['keys']
        latest = str(max(int(k) for k in keys.keys()))
        pub_b64 = keys[latest].get('public_key', '')
        if pub_b64:
            # PEM or base64 raw depending on Vault version
            if 'BEGIN' in pub_b64:
                from cryptography.hazmat.primitives.serialization import load_pem_public_key, Encoding, PublicFormat
                key = load_pem_public_key(pub_b64.encode())
                return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
            return base64.b64decode(pub_b64).hex()
        return ''

    def is_available(self) -> bool:
        if not self._addr or not self._token:
            return False
        try:
            client = self._get_client()
            return client.is_authenticated()
        except Exception:
            return False

    def get_provider_name(self) -> str:
        return 'hashicorp_vault'


# ═══════════════════════════════════════════════════════════════
# Environment Variable Fallback (DEV ONLY)
# ═══════════════════════════════════════════════════════════════

class EnvVarFallbackProvider(HSMProvider):
    """DEVELOPMENT ONLY. Loads private key from environment variable.

    ⚠️  This is NOT hardware-protected. The private key exists in process memory.
    ⚠️  Only for local development and testing.
    ⚠️  In production, use a real HSM provider.
    """

    def __init__(self):
        super().__init__()
        self._warned = False

    def _warn_once(self):
        if not self._warned:
            self._warned = True
            import sys
            msg = (
                "\n" + "=" * 70 +
                "\n  WARNING: Master key loaded from environment variable."
                "\n  This is NOT hardware-protected. Use an HSM in production."
                "\n  Set HART_GCP_KMS_KEY_PATH, HART_AZURE_VAULT_URL, or"
                "\n  HART_VAULT_ADDR to enable HSM protection."
                "\n" + "=" * 70 + "\n"
            )
            print(msg, file=sys.stderr)
            logger.warning("Master key signing via env var fallback - NOT HSM-protected")

    def sign(self, payload_bytes: bytes) -> bytes:
        self._warn_once()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        hex_key = os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', '')
        if not hex_key:
            raise HSMSigningError(
                'No HSM configured and HEVOLVE_MASTER_PRIVATE_KEY not set. '
                'Cannot sign without master key.')
        try:
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(hex_key))
            return priv.sign(payload_bytes)
        except Exception as e:
            raise HSMSigningError(f'Env var signing failed: {e}')

    def get_public_key_hex(self) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        hex_key = os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', '')
        if not hex_key:
            return ''
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(hex_key))
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def is_available(self) -> bool:
        return bool(os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY', ''))

    def get_provider_name(self) -> str:
        return 'env_var_fallback_DEV_ONLY'


# ═══════════════════════════════════════════════════════════════
# Provider Registry & Singleton
# ═══════════════════════════════════════════════════════════════

# Priority order: real HSMs first, env var fallback last
_PROVIDER_CLASSES = [
    GoogleCloudKMSProvider,
    AzureKeyVaultProvider,
    VaultTransitProvider,
    EnvVarFallbackProvider,
]

_active_provider: Optional[HSMProvider] = None
_provider_lock = threading.Lock()


def get_hsm_provider() -> HSMProvider:
    """Get the active HSM provider. Auto-selects based on available config.
    Priority: GCP KMS > Azure Key Vault > HashiCorp Vault > env var fallback.
    Raises HSMUnavailableError if nothing is configured."""
    global _active_provider
    if _active_provider is not None:
        return _active_provider

    with _provider_lock:
        if _active_provider is not None:
            return _active_provider

        for cls in _PROVIDER_CLASSES:
            try:
                provider = cls()
                if provider.is_available():
                    # Verify public key matches trust anchor
                    from security.master_key import MASTER_PUBLIC_KEY_HEX
                    hsm_pub = provider.get_public_key_hex()
                    if hsm_pub and hsm_pub != MASTER_PUBLIC_KEY_HEX:
                        logger.error(
                            f"HSM provider {provider.get_provider_name()}: "
                            f"public key mismatch! HSM={hsm_pub[:16]}... "
                            f"expected={MASTER_PUBLIC_KEY_HEX[:16]}...")
                        continue  # Wrong key - try next provider

                    _active_provider = provider
                    logger.info(f"HSM provider active: {provider.get_provider_name()}")
                    return provider
            except Exception as e:
                logger.debug(f"HSM provider {cls.__name__} unavailable: {e}")

        raise HSMUnavailableError(
            'No HSM backend available. Configure one of: '
            'HART_GCP_KMS_KEY_PATH (Google), '
            'HART_AZURE_VAULT_URL (Azure), '
            'HART_VAULT_ADDR (HashiCorp Vault), '
            'or HEVOLVE_MASTER_PRIVATE_KEY (dev fallback).')


def is_hsm_available() -> bool:
    """Check if any HSM provider is available (without raising)."""
    try:
        get_hsm_provider()
        return True
    except HSMUnavailableError:
        return False


def get_hsm_status() -> Dict:
    """Return HSM status for dashboards and health checks."""
    try:
        provider = get_hsm_provider()
        return {
            'available': True,
            'provider': provider.get_provider_name(),
            'hardware_backed': not isinstance(provider, EnvVarFallbackProvider),
            'sign_count': provider._sign_count,
            'audit_entries': len(provider._audit_log),
        }
    except HSMUnavailableError:
        return {
            'available': False,
            'provider': None,
            'hardware_backed': False,
            'sign_count': 0,
            'audit_entries': 0,
        }


def hsm_sign_payload(payload: dict) -> str:
    """Sign a JSON payload via the active HSM. Returns hex signature.
    The private key NEVER leaves the HSM hardware."""
    provider = get_hsm_provider()
    return provider.sign_json_payload(payload)
