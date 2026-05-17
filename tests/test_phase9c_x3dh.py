"""Phase 9.C — X3DH initial key agreement.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Coverage of the cryptographic invariants that matter for the
handshake:
  - Round-trip: initiator + responder with the same bundle derive
    IDENTICAL shared_secrets.
  - Without OPK: 3-DH variant still round-trips.
  - SPK signature gate: a tampered SPK signature raises
    X3DHSignatureError; verify_sig=False bypasses (legacy path).
  - Wrong identity sign pub fails the signature check.
  - Wrong ephemeral / wrong identity priv produces a DIFFERENT
    shared_secret on responder side (ECDH only matches when both
    sides start from the agreed bundle).
  - PreKeyMessage wire format round-trip is identity.
  - OPK consumption: responder MUST receive the matching OPK_priv
    when opk_id is present, else raises.
  - HKDF output is exactly 32 bytes.

Skipped when `cryptography` isn't installed.
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from integrations.social import e2e_x3dh as x3dh

needs_crypto = pytest.mark.skipif(
    not x3dh._HAS_CRYPTO,
    reason="cryptography package not installed; pip install cryptography")


# ── Helpers ─────────────────────────────────────────────────────────


def _build_bob_bundle(*, with_opk: bool = True):
    """Helper: build Bob's full prekey bundle + the matching priv
    set (which the responder side needs to recompute the shared
    secret).  Returns (bundle, privs_dict)."""
    ik_sign_priv, ik_sign_pub = x3dh.generate_ed25519_keypair()
    ik_priv,      ik_pub      = x3dh.generate_x25519_keypair()
    spk_priv,     spk_pub     = x3dh.generate_x25519_keypair()
    spk_sig = x3dh.sign_prekey(ik_sign_priv, spk_pub)
    opk_priv = opk_pub = None
    opk_id = None
    if with_opk:
        opk_priv, opk_pub = x3dh.generate_x25519_keypair()
        opk_id = 'opk-bob-1'
    bundle = x3dh.PreKeyBundle(
        identity_pub=ik_pub,
        identity_sign_pub=ik_sign_pub,
        signed_prekey_pub=spk_pub,
        signed_prekey_sig=spk_sig,
        one_time_prekey_pub=opk_pub,
        one_time_prekey_id=opk_id,
    )
    privs = {
        'identity_sign_priv': ik_sign_priv,
        'identity_priv': ik_priv,
        'signed_prekey_priv': spk_priv,
        'one_time_prekey_priv': opk_priv,
    }
    return bundle, privs


def _build_alice_keys():
    """Alice's long-lived identity keypair.  She generates the
    ephemeral inside initiator_derive_shared_secret."""
    return x3dh.generate_x25519_keypair()


# ── Round-trip ──────────────────────────────────────────────────────


@needs_crypto
def test_x3dh_round_trip_with_opk():
    bundle, privs = _build_bob_bundle(with_opk=True)
    alice_ik_priv, alice_ik_pub = _build_alice_keys()

    sk_a, ek_priv, ek_pub = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle)

    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=alice_ik_pub,
        ephemeral_pub=ek_pub,
        one_time_prekey_id=bundle.one_time_prekey_id,
        ratchet_envelope_blob=b'first-ratchet-envelope-stub',
    )

    sk_b = x3dh.responder_derive_shared_secret(
        privs['identity_priv'], privs['signed_prekey_priv'], msg,
        one_time_prekey_priv=privs['one_time_prekey_priv'])

    assert sk_a == sk_b, "X3DH round-trip must match by ECDH symmetry"
    assert len(sk_a) == 32


@needs_crypto
def test_x3dh_round_trip_without_opk():
    """3-DH variant: responder didn't publish a one-time prekey."""
    bundle, privs = _build_bob_bundle(with_opk=False)
    alice_ik_priv, alice_ik_pub = _build_alice_keys()

    sk_a, ek_priv, ek_pub = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle)
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=alice_ik_pub,
        ephemeral_pub=ek_pub,
        one_time_prekey_id=None,
        ratchet_envelope_blob=b'',
    )
    sk_b = x3dh.responder_derive_shared_secret(
        privs['identity_priv'], privs['signed_prekey_priv'], msg)
    assert sk_a == sk_b


# ── Signature gates ─────────────────────────────────────────────────


@needs_crypto
def test_tampered_spk_sig_raises():
    bundle, privs = _build_bob_bundle()
    # Flip a byte in the signature.
    bad_sig = bytearray(bundle.signed_prekey_sig)
    bad_sig[0] ^= 0xff
    bad_bundle = bundle._replace(signed_prekey_sig=bytes(bad_sig))
    alice_ik_priv, _ = _build_alice_keys()
    with pytest.raises(x3dh.X3DHSignatureError):
        x3dh.initiator_derive_shared_secret(alice_ik_priv, bad_bundle)


@needs_crypto
def test_wrong_identity_sign_pub_rejects():
    """An attacker swapping IK_sign_pub for theirs while keeping the
    real SPK + signature can't pass — because the signature was made
    by the real IK_sign_priv, not the attacker's."""
    bundle, _ = _build_bob_bundle()
    _, fake_sign_pub = x3dh.generate_ed25519_keypair()
    bad_bundle = bundle._replace(identity_sign_pub=fake_sign_pub)
    alice_ik_priv, _ = _build_alice_keys()
    with pytest.raises(x3dh.X3DHSignatureError):
        x3dh.initiator_derive_shared_secret(alice_ik_priv, bad_bundle)


@needs_crypto
def test_verify_sig_false_skips_check():
    """verify_sig=False bypasses signature check (legacy path).
    Should NOT be used in production, but the seam exists for
    bundles that pre-date signing."""
    bundle, privs = _build_bob_bundle()
    bad_sig = bytearray(bundle.signed_prekey_sig)
    bad_sig[0] ^= 0xff
    bad_bundle = bundle._replace(signed_prekey_sig=bytes(bad_sig))
    alice_ik_priv, _ = _build_alice_keys()
    # Doesn't raise — caller chose to bypass.
    sk, _, _ = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bad_bundle, verify_sig=False)
    assert len(sk) == 32


# ── Mismatch / divergence ───────────────────────────────────────────


@needs_crypto
def test_responder_with_wrong_opk_priv_diverges():
    """If the responder hands a DIFFERENT OPK_priv than the one whose
    pub was in the bundle, the derived secret diverges from the
    initiator's.  Tests that the OPK is genuinely binding, not
    decorative."""
    bundle, privs = _build_bob_bundle(with_opk=True)
    alice_ik_priv, alice_ik_pub = _build_alice_keys()
    sk_a, ek_priv, ek_pub = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle)
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=alice_ik_pub,
        ephemeral_pub=ek_pub,
        one_time_prekey_id=bundle.one_time_prekey_id,
        ratchet_envelope_blob=b'',
    )
    # Wrong OPK priv (random fresh keypair).
    wrong_opk_priv, _ = x3dh.generate_x25519_keypair()
    sk_b = x3dh.responder_derive_shared_secret(
        privs['identity_priv'], privs['signed_prekey_priv'], msg,
        one_time_prekey_priv=wrong_opk_priv)
    assert sk_a != sk_b


@needs_crypto
def test_responder_missing_opk_priv_when_required_raises():
    bundle, privs = _build_bob_bundle(with_opk=True)
    alice_ik_priv, alice_ik_pub = _build_alice_keys()
    _, ek_priv, ek_pub = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle)
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=alice_ik_pub,
        ephemeral_pub=ek_pub,
        one_time_prekey_id=bundle.one_time_prekey_id,
        ratchet_envelope_blob=b'',
    )
    with pytest.raises(x3dh.X3DHError):
        x3dh.responder_derive_shared_secret(
            privs['identity_priv'],
            privs['signed_prekey_priv'],
            msg, one_time_prekey_priv=None)


# ── Wire format ─────────────────────────────────────────────────────


@needs_crypto
def test_prekey_message_wire_round_trip_with_opk():
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=b'\x01' * 32,
        ephemeral_pub=b'\x02' * 32,
        one_time_prekey_id='opk-1',
        ratchet_envelope_blob=b'opaque-envelope-bytes',
    )
    blob = x3dh.serialize_prekey_message(msg)
    parsed = x3dh.deserialize_prekey_message(blob)
    assert parsed == msg


@needs_crypto
def test_prekey_message_wire_round_trip_without_opk():
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=b'\x03' * 32,
        ephemeral_pub=b'\x04' * 32,
        one_time_prekey_id=None,
        ratchet_envelope_blob=b'\x99' * 100,
    )
    blob = x3dh.serialize_prekey_message(msg)
    parsed = x3dh.deserialize_prekey_message(blob)
    assert parsed == msg


@needs_crypto
def test_prekey_message_wire_unknown_version_raises():
    blob = bytes([99, 0, 32]) + b'\x00' * 32 + bytes([32]) + b'\x00' * 32 \
           + bytes([0, 0, 0, 0])
    with pytest.raises(x3dh.X3DHError):
        x3dh.deserialize_prekey_message(blob)


# ── Determinism / sanity ───────────────────────────────────────────


@needs_crypto
def test_kdf_output_length_is_32():
    sk = x3dh._x3dh_kdf(b'arbitrary' * 100)
    assert len(sk) == 32


@needs_crypto
def test_two_alice_sessions_produce_different_secrets():
    """Each session uses a fresh ephemeral; two handshakes from
    Alice → Bob with the same bundle MUST produce distinct shared
    secrets (forward secrecy invariant)."""
    bundle, _ = _build_bob_bundle(with_opk=False)
    alice_ik_priv, _ = _build_alice_keys()
    sk1, _, _ = x3dh.initiator_derive_shared_secret(alice_ik_priv, bundle)
    sk2, _, _ = x3dh.initiator_derive_shared_secret(alice_ik_priv, bundle)
    assert sk1 != sk2


@needs_crypto
def test_explicit_ephemeral_priv_round_trips():
    """ephemeral_priv override is a testing seam: passing a fixed
    priv must produce a deterministic shared_secret given a fixed
    bundle.  Used by tests that need reproducible sessions."""
    bundle, privs = _build_bob_bundle(with_opk=False)
    alice_ik_priv, alice_ik_pub = _build_alice_keys()
    fixed_ek_priv, _ = x3dh.generate_x25519_keypair()
    sk_a1, _, ep1 = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle, ephemeral_priv=fixed_ek_priv)
    sk_a2, _, ep2 = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle, ephemeral_priv=fixed_ek_priv)
    assert sk_a1 == sk_a2
    assert ep1 == ep2


# ── Bare-deploy gate ───────────────────────────────────────────────


def test_unavailable_when_no_crypto(monkeypatch):
    monkeypatch.setattr(x3dh, '_HAS_CRYPTO', False)
    with pytest.raises(x3dh.X3DHUnavailable):
        x3dh._require_crypto()


def test_module_imports_cleanly():
    from integrations.social import e2e_x3dh  # noqa: F401
    assert hasattr(e2e_x3dh, 'initiator_derive_shared_secret')
    assert hasattr(e2e_x3dh, 'responder_derive_shared_secret')
