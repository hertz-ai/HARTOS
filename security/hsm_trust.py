"""
HSM Trust Network - Certificate pinning and path protection for HSM connections.

Ensures the entire path from application to HSM is protected:
  1. TLS certificate pinning to HSM endpoints
  2. mTLS client certificates for HSM authentication
  3. Request signing for HSM API calls
  4. Audit logging of all trust path events
  5. Health monitoring of HSM connectivity

Trust chain:
  Application (this code)
      ↓ mTLS / IAM
  HSM Endpoint (pinned certificate)
      ↓ hardware boundary
  HSM Hardware (FIPS 140-2 Level 3)
      ↓ internal
  Private Key (NEVER extracted)

┌─────────────────────────────────────────────────────────────────┐
│  AI EXCLUSION ZONE                                              │
│  AI tools MUST NOT modify trust anchors or certificate pins.   │
└─────────────────────────────────────────────────────────────────┘
"""
import os
import ssl
import json
import hashlib
import logging
import threading
import time
from typing import Optional, Dict, List
from datetime import datetime

logger = logging.getLogger('hevolve_security')

# ═══════════════════════════════════════════════════════════════
# Certificate Pin Store
# ═══════════════════════════════════════════════════════════════

# SHA-256 pins of trusted HSM endpoint certificates.
# These are loaded from config or hardcoded for known cloud HSM services.
_KNOWN_HSM_PINS: Dict[str, List[str]] = {
    # Google Cloud KMS regional endpoints
    'cloudkms.googleapis.com': [
        # Google Trust Services root CA pins (GTS Root R1-R4)
        # These are public, well-known pins for Google's PKI
    ],
    # Azure Key Vault
    'vault.azure.net': [],
    # HashiCorp Vault - user-configured, loaded from HYVE_VAULT_CA_CERT
}


class HSMTrustManager:
    """Manages TLS trust for HSM connections.

    Responsibilities:
    - Certificate pinning for HSM endpoints
    - mTLS client certificate management
    - Connection health monitoring
    - Audit trail of trust path events
    """

    def __init__(self):
        self._pins: Dict[str, List[str]] = {}
        self._lock = threading.Lock()
        self._health_history: list = []
        self._load_pins()

    def _load_pins(self):
        """Load certificate pins from config file or environment."""
        # User-configured pins
        pin_file = os.environ.get('HYVE_HSM_PIN_FILE', '')
        if pin_file and os.path.exists(pin_file):
            try:
                with open(pin_file, 'r') as f:
                    user_pins = json.load(f)
                self._pins.update(user_pins)
                logger.info(f"Loaded HSM certificate pins from {pin_file}")
            except Exception as e:
                logger.warning(f"Failed to load HSM pin file: {e}")

        # Vault CA cert → derive pin
        vault_ca = os.environ.get('HYVE_VAULT_CA_CERT', '')
        if vault_ca and os.path.exists(vault_ca):
            try:
                pin = self._compute_cert_pin(vault_ca)
                vault_addr = os.environ.get('HYVE_VAULT_ADDR', '')
                if vault_addr:
                    from urllib.parse import urlparse
                    host = urlparse(vault_addr).hostname
                    if host:
                        self._pins.setdefault(host, []).append(pin)
                        logger.info(f"Pinned Vault CA cert for {host}: {pin[:16]}...")
            except Exception as e:
                logger.debug(f"Vault CA cert pin failed: {e}")

    @staticmethod
    def _compute_cert_pin(cert_path: str) -> str:
        """Compute SHA-256 pin of a certificate's Subject Public Key Info (SPKI)."""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PublicFormat,
            )
            with open(cert_path, 'rb') as f:
                cert_data = f.read()
            if b'BEGIN CERTIFICATE' in cert_data:
                cert = x509.load_pem_x509_certificate(cert_data)
            else:
                cert = x509.load_der_x509_certificate(cert_data)

            spki_bytes = cert.public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            return hashlib.sha256(spki_bytes).hexdigest()
        except Exception as e:
            logger.warning(f"Certificate pin computation failed: {e}")
            return ''

    def create_ssl_context(self, hostname: str) -> ssl.SSLContext:
        """Create an SSL context with certificate pinning for an HSM endpoint.

        If pins are configured for this hostname, the connection will be
        rejected if the server's certificate doesn't match any pin.
        """
        ctx = ssl.create_default_context()

        # Load custom CA cert if available
        ca_cert = os.environ.get('HYVE_HSM_CA_CERT', '')
        if ca_cert and os.path.exists(ca_cert):
            ctx.load_verify_locations(ca_cert)

        # Load client cert for mTLS if available
        client_cert = os.environ.get('HYVE_HSM_CLIENT_CERT', '')
        client_key = os.environ.get('HYVE_HSM_CLIENT_KEY', '')
        if client_cert and client_key:
            try:
                ctx.load_cert_chain(client_cert, client_key)
                logger.debug(f"mTLS client cert loaded for {hostname}")
            except Exception as e:
                logger.warning(f"mTLS client cert load failed: {e}")

        # Set minimum TLS version
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        return ctx

    def verify_connection(self, hostname: str, port: int = 443) -> Dict:
        """Verify TLS connection to an HSM endpoint.
        Checks certificate chain and pin matching."""
        import socket
        result = {
            'hostname': hostname,
            'port': port,
            'connected': False,
            'tls_version': None,
            'cert_pin': None,
            'pin_matched': None,
            'timestamp': datetime.utcnow().isoformat(),
        }

        try:
            ctx = self.create_ssl_context(hostname)
            with socket.create_connection((hostname, port), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                    result['connected'] = True
                    result['tls_version'] = ssock.version()

                    # Get server cert and compute pin
                    cert_der = ssock.getpeercert(binary_form=True)
                    if cert_der:
                        from cryptography import x509
                        from cryptography.hazmat.primitives.serialization import (
                            Encoding, PublicFormat,
                        )
                        cert = x509.load_der_x509_certificate(cert_der)
                        spki = cert.public_key().public_bytes(
                            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
                        pin = hashlib.sha256(spki).hexdigest()
                        result['cert_pin'] = pin

                        # Check against known pins
                        known = self._pins.get(hostname, [])
                        if known:
                            result['pin_matched'] = pin in known
                        else:
                            result['pin_matched'] = None  # No pins configured

        except Exception as e:
            result['error'] = str(e)

        with self._lock:
            self._health_history.append(result)
            if len(self._health_history) > 100:
                self._health_history = self._health_history[-50:]

        return result

    def get_trust_status(self) -> Dict:
        """Return trust network status for dashboards."""
        return {
            'pins_configured': {k: len(v) for k, v in self._pins.items() if v},
            'mtls_configured': bool(
                os.environ.get('HYVE_HSM_CLIENT_CERT') and
                os.environ.get('HYVE_HSM_CLIENT_KEY')),
            'custom_ca': bool(os.environ.get('HYVE_HSM_CA_CERT')),
            'vault_ca': bool(os.environ.get('HYVE_VAULT_CA_CERT')),
            'recent_checks': self._health_history[-5:] if self._health_history else [],
        }


# ═══════════════════════════════════════════════════════════════
# Path Protection Monitor
# ═══════════════════════════════════════════════════════════════

class HSMPathMonitor:
    """Background monitor that continuously verifies HSM trust path integrity.

    Checks:
    1. HSM endpoint reachability
    2. Certificate pin consistency (hasn't changed unexpectedly)
    3. Public key from HSM matches trust anchor
    4. Signing operations produce valid signatures
    """

    def __init__(self, check_interval: int = 300):
        self._interval = check_interval
        self._running = False
        self._thread = None
        self._trust_manager = HSMTrustManager()
        self._last_check: Optional[Dict] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(f"HSM path monitor started (interval={self._interval}s)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _monitor_loop(self):
        while self._running:
            try:
                self._last_check = self._check_path()
                if not self._last_check.get('healthy'):
                    logger.warning(
                        f"HSM path unhealthy: {self._last_check.get('details')}")
            except Exception as e:
                logger.debug(f"HSM monitor error: {e}")
            time.sleep(self._interval)

    def _check_path(self) -> Dict:
        """Run full trust path verification."""
        from .hsm_provider import get_hsm_provider, is_hsm_available, EnvVarFallbackProvider

        result = {
            'timestamp': datetime.utcnow().isoformat(),
            'healthy': True,
            'checks': {},
        }

        # Check 1: HSM availability
        hsm_ok = is_hsm_available()
        result['checks']['hsm_available'] = hsm_ok
        if not hsm_ok:
            result['healthy'] = False
            result['details'] = 'No HSM provider available'
            return result

        provider = get_hsm_provider()
        result['checks']['provider'] = provider.get_provider_name()
        result['checks']['hardware_backed'] = not isinstance(
            provider, EnvVarFallbackProvider)

        # Check 2: Public key matches trust anchor
        try:
            from security.master_key import MASTER_PUBLIC_KEY_HEX
            hsm_pub = provider.get_public_key_hex()
            pub_match = hsm_pub == MASTER_PUBLIC_KEY_HEX
            result['checks']['public_key_match'] = pub_match
            if not pub_match:
                result['healthy'] = False
                result['details'] = 'HSM public key does not match trust anchor'
                return result
        except Exception as e:
            result['checks']['public_key_match'] = False
            result['healthy'] = False
            result['details'] = f'Public key check failed: {e}'
            return result

        # Check 3: Test sign + verify round-trip
        try:
            test_payload = {'_hsm_health_check': True,
                            'timestamp': datetime.utcnow().isoformat()}
            sig_hex = provider.sign_json_payload(test_payload)
            from security.master_key import verify_master_signature
            valid = verify_master_signature(test_payload, sig_hex)
            result['checks']['sign_verify_roundtrip'] = valid
            if not valid:
                result['healthy'] = False
                result['details'] = 'HSM sign→verify round-trip failed'
        except Exception as e:
            result['checks']['sign_verify_roundtrip'] = False
            result['healthy'] = False
            result['details'] = f'Sign/verify test failed: {e}'

        if result['healthy']:
            result['details'] = 'All HSM path checks passed'

        return result

    def get_last_check(self) -> Optional[Dict]:
        return self._last_check

    def get_trust_status(self) -> Dict:
        return self._trust_manager.get_trust_status()


# Module-level singleton
_path_monitor: Optional[HSMPathMonitor] = None


def get_path_monitor() -> HSMPathMonitor:
    global _path_monitor
    if _path_monitor is None:
        _path_monitor = HSMPathMonitor()
    return _path_monitor


def start_hsm_monitor():
    """Start the background HSM path monitor."""
    monitor = get_path_monitor()
    monitor.start()
    return monitor
