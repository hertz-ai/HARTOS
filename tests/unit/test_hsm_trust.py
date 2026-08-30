"""Behavioural tests for security.hsm_trust — the HSM trust-path (cert pinning,
mTLS, connection health).

This is a security-critical module that was 0% covered. It protects the path
Application -> HSM endpoint (pinned cert / mTLS) so a MITM cannot impersonate the
HSM. These tests VERIFY the pin computation and pin/SSL/trust-status logic — they
do NOT modify any trust anchor or pin (the module's AI-exclusion note forbids
modifying those, not testing them), and they never load or exercise private-key
material: HSMPathMonitor is tested only on its is_hsm_available()==False path via
a mock, so no provider signing is ever triggered.

`cryptography` is used to mint a throwaway self-signed cert for the pin math.
"""
from __future__ import annotations

import hashlib
import json
import ssl
from datetime import datetime, timedelta

import pytest

from security.hsm_trust import HSMTrustManager, HSMPathMonitor

_HSM_ENV = (
    'HART_HSM_PIN_FILE', 'HART_VAULT_CA_CERT', 'HART_VAULT_ADDR',
    'HART_HSM_CA_CERT', 'HART_HSM_CLIENT_CERT', 'HART_HSM_CLIENT_KEY',
)


@pytest.fixture(autouse=True)
def _clean_hsm_env(monkeypatch):
    # Deterministic: no ambient HSM config leaks into the manager under test.
    for k in _HSM_ENV:
        monkeypatch.delenv(k, raising=False)


def _make_self_signed(tmp_path):
    """Mint a throwaway EC self-signed cert; return (pem_path, expected_spki_pin)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test-hsm')])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(minutes=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    p = tmp_path / 'hsm.pem'
    p.write_bytes(pem)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return str(p), hashlib.sha256(spki).hexdigest()


# ── _compute_cert_pin: the pin IS the SPKI SHA-256 ──────────────────────────
def test_compute_cert_pin_is_spki_sha256(tmp_path):
    pytest.importorskip('cryptography')
    cert_path, expected = _make_self_signed(tmp_path)
    pin = HSMTrustManager._compute_cert_pin(cert_path)
    assert pin == expected
    assert len(pin) == 64 and all(c in '0123456789abcdef' for c in pin)


def test_compute_cert_pin_is_deterministic(tmp_path):
    pytest.importorskip('cryptography')
    cert_path, _ = _make_self_signed(tmp_path)
    assert HSMTrustManager._compute_cert_pin(cert_path) == \
        HSMTrustManager._compute_cert_pin(cert_path)


def test_compute_cert_pin_bad_path_returns_empty():
    # Never raise on a missing/garbage cert — returns '' so callers skip pinning.
    assert HSMTrustManager._compute_cert_pin('/no/such/cert.pem') == ''


# ── _load_pins: user pin file + Vault CA derivation ─────────────────────────
def test_load_pins_from_pin_file(tmp_path, monkeypatch):
    pin_file = tmp_path / 'pins.json'
    pin_file.write_text(json.dumps({'cloudkms.googleapis.com': ['abc123']}))
    monkeypatch.setenv('HART_HSM_PIN_FILE', str(pin_file))
    mgr = HSMTrustManager()
    assert mgr._pins.get('cloudkms.googleapis.com') == ['abc123']


def test_load_pins_bad_file_does_not_crash(tmp_path, monkeypatch):
    bad = tmp_path / 'pins.json'
    bad.write_text('{not valid json')
    monkeypatch.setenv('HART_HSM_PIN_FILE', str(bad))
    mgr = HSMTrustManager()  # must not raise
    assert isinstance(mgr._pins, dict)


def test_load_pins_derives_vault_pin(tmp_path, monkeypatch):
    pytest.importorskip('cryptography')
    cert_path, expected = _make_self_signed(tmp_path)
    monkeypatch.setenv('HART_VAULT_CA_CERT', cert_path)
    monkeypatch.setenv('HART_VAULT_ADDR', 'https://vault.example.com:8200')
    mgr = HSMTrustManager()
    assert expected in mgr._pins.get('vault.example.com', [])


# ── create_ssl_context: enforces TLS 1.2 floor, returns a real context ──────
def test_create_ssl_context_enforces_tls12():
    ctx = HSMTrustManager().create_ssl_context('cloudkms.googleapis.com')
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_create_ssl_context_survives_bad_client_cert(tmp_path, monkeypatch):
    # A broken mTLS cert must warn + still return a context, not raise.
    monkeypatch.setenv('HART_HSM_CLIENT_CERT', str(tmp_path / 'missing.crt'))
    monkeypatch.setenv('HART_HSM_CLIENT_KEY', str(tmp_path / 'missing.key'))
    ctx = HSMTrustManager().create_ssl_context('vault.azure.net')
    assert isinstance(ctx, ssl.SSLContext)


# ── verify_connection: graceful failure + bounded health history ────────────
def test_verify_connection_unreachable_is_graceful():
    mgr = HSMTrustManager()
    r = mgr.verify_connection('127.0.0.1', port=1)  # refused instantly
    assert r['connected'] is False and 'error' in r
    assert r['hostname'] == '127.0.0.1' and r['port'] == 1


def test_health_history_is_bounded():
    mgr = HSMTrustManager()
    # Pre-seed over the cap; a fresh check must trim to <= 100 (the code keeps
    # the last 50 once it exceeds 100).
    mgr._health_history = [{'i': i} for i in range(120)]
    mgr.verify_connection('127.0.0.1', port=1)
    assert len(mgr._health_history) <= 100


# ── get_trust_status: dashboard shape ───────────────────────────────────────
def test_get_trust_status_shape(tmp_path, monkeypatch):
    pin_file = tmp_path / 'pins.json'
    pin_file.write_text(json.dumps({'vault.azure.net': ['deadbeef']}))
    monkeypatch.setenv('HART_HSM_PIN_FILE', str(pin_file))
    status = HSMTrustManager().get_trust_status()
    assert set(status) >= {
        'pins_configured', 'mtls_configured', 'custom_ca', 'vault_ca',
        'recent_checks'}
    assert status['pins_configured'].get('vault.azure.net') == 1
    assert status['mtls_configured'] is False  # env cleared by fixture


# ── HSMPathMonitor: lifecycle + the no-HSM path (no key material touched) ────
def test_monitor_start_stop_lifecycle(monkeypatch):
    mon = HSMPathMonitor(check_interval=300)
    # Neutralise the real path check so start() spins a harmless loop.
    monkeypatch.setattr(mon, '_check_path', lambda: {'healthy': True})
    mon.start()
    assert mon._running is True
    mon.stop()
    assert mon._running is False


def test_check_path_unhealthy_when_no_hsm(monkeypatch):
    # is_hsm_available()==False must short-circuit to unhealthy BEFORE any
    # provider is fetched or any signing happens — so no private key is loaded.
    import security.hsm_provider as prov
    monkeypatch.setattr(prov, 'is_hsm_available', lambda: False)
    result = HSMPathMonitor(check_interval=300)._check_path()
    assert result['healthy'] is False
    assert result['checks']['hsm_available'] is False
    assert 'No HSM provider' in result['details']
