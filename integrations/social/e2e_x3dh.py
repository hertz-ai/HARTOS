"""
HevolveSocial — Phase 9.C X3DH initial key agreement.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Implements libsignal-style X3DH (Extended Triple Diffie-Hellman) so
two parties can derive a Double Ratchet initial shared_secret
without an out-of-band exchange.  Replaces the deterministic
placeholder in e2e_state_repo._derive_initial_shared_secret.

Protocol summary
================

Each user publishes a prekey bundle:
  - IK_pub:   long-lived identity X25519 public key
  - IK_sign_pub: long-lived identity Ed25519 public key (used to
                  sign SPK; same root identity but separate keypair
                  because X25519 + Ed25519 are different curves)
  - SPK_pub:  signed prekey X25519 public key (rotated ~weekly)
  - SPK_sig:  Ed25519 signature over SPK_pub bytes by IK_sign_priv
  - OPK_pub:  optional one-time prekey X25519 public key (used once
              and discarded by the responder)

Alice (initiator) handshake:
  1. Fetch Bob's bundle.  Verify SPK_sig with IK_sign_pub.
  2. Generate ephemeral keypair (EK_priv_A, EK_pub_A).
  3. Compute four DHs:
       DH1 = DH(IK_priv_A,  SPK_pub_B)
       DH2 = DH(EK_priv_A,  IK_pub_B)
       DH3 = DH(EK_priv_A,  SPK_pub_B)
       DH4 = DH(EK_priv_A,  OPK_pub_B)   [omitted if no OPK]
  4. SK = HKDF-SHA256(DH1 || DH2 || DH3 [|| DH4],
                       info='hevolve-x3dh-v1', salt=zeroed)
  5. Send to Bob a `PreKeyMessage`:
       - IK_pub_A, EK_pub_A, OPK_id_consumed (or None)
       - the first Double-Ratchet envelope encrypted under SK

Bob (responder) handshake:
  1. On receiving a PreKeyMessage, look up OPK_priv_B by OPK_id and
     DELETE it from the prekey store (one-time semantics).
  2. Recompute DH1..DH4 with private keys reversed (ECDH symmetry):
       DH1' = DH(SPK_priv_B, IK_pub_A)
       DH2' = DH(IK_priv_B,  EK_pub_A)
       DH3' = DH(SPK_priv_B, EK_pub_A)
       DH4' = DH(OPK_priv_B, EK_pub_A)
     By X25519 symmetry, DH1==DH1', etc., so SK matches.
  3. Initialize ratchet with SK; decrypt the first envelope.

Forward secrecy
===============
EK_priv_A is ephemeral (lives one session) and OPK_priv_B is
one-time (deleted on use).  Compromise of either party's IK or SPK
LATER does NOT decrypt past sessions, because EK + OPK private bits
are gone.  This is the property the Phase 9.B placeholder lacks.

Scope of THIS file
==================
Pure-compute primitives.  No schema, no endpoints, no wiring into
ConversationService.send_message — those are integration follow-ups
(Phase 9.D) that need:
  - prekey_bundles table (IK + SPK + OPKs per user)
  - one-time prekey delivery + deletion endpoints
  - PreKeyMessage envelope routing alongside RatchetEnvelope
"""

from __future__ import annotations

import os
import struct
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class X3DHError(Exception):
    """Base class for X3DH-level errors."""


class X3DHUnavailable(X3DHError):
    """Raised when `cryptography` isn't installed."""


class X3DHSignatureError(X3DHError):
    """Raised when SPK signature verification fails."""


# Try to import the cryptography primitives.  If absent, every API
# raises X3DHUnavailable so the caller can flag-gate.
try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (  # type: ignore
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore
    from cryptography.hazmat.primitives import hashes, serialization  # type: ignore
    from cryptography.exceptions import InvalidSignature  # type: ignore
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover — exercised only on bare deploys
    _HAS_CRYPTO = False
    X25519PrivateKey = X25519PublicKey = None
    Ed25519PrivateKey = Ed25519PublicKey = None
    HKDF = hashes = serialization = None
    InvalidSignature = Exception


SHARED_SECRET_BYTES = 32
HKDF_INFO = b'hevolve-x3dh-v1'
# X3DH per-spec uses a 32-byte zero salt prefix on the HKDF input
# to disambiguate from random key material derived elsewhere.
HKDF_SALT_PREFIX = b'\xff' * 32


def _require_crypto() -> None:
    if not _HAS_CRYPTO:
        raise X3DHUnavailable(
            "X3DH requires the `cryptography` package; "
            "install via `pip install cryptography` or skip 9.C bootstrap")


# ── Keypair helpers ────────────────────────────────────────────────


def _x25519_priv_from_bytes(b: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(b)


def _x25519_pub_from_bytes(b: bytes) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(b)


def _ed25519_priv_from_bytes(b: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b)


def _ed25519_pub_from_bytes(b: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b)


def _x25519_pub_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def generate_x25519_keypair() -> Tuple[bytes, bytes]:
    """Returns (private_bytes, public_bytes) — both raw 32-byte
    X25519 representations."""
    _require_crypto()
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = _x25519_pub_bytes(priv)
    return priv_bytes, pub_bytes


def generate_ed25519_keypair() -> Tuple[bytes, bytes]:
    """Returns (private_bytes, public_bytes) — both raw 32-byte
    Ed25519 representations."""
    _require_crypto()
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_bytes, pub_bytes


def _dh(our_priv: bytes, their_pub: bytes) -> bytes:
    return _x25519_priv_from_bytes(our_priv).exchange(
        _x25519_pub_from_bytes(their_pub))


# ── Signatures ─────────────────────────────────────────────────────


def sign_prekey(identity_sign_priv: bytes,
                signed_prekey_pub: bytes) -> bytes:
    """Sign the SPK pub bytes with the identity Ed25519 priv.
    Returns the 64-byte Ed25519 signature."""
    _require_crypto()
    return _ed25519_priv_from_bytes(identity_sign_priv).sign(
        signed_prekey_pub)


def verify_prekey_signature(identity_sign_pub: bytes,
                            signed_prekey_pub: bytes,
                            signature: bytes) -> bool:
    """Returns True iff `signature` is a valid Ed25519 sig over
    `signed_prekey_pub` by `identity_sign_pub`.  No exception on
    failure — caller decides whether to raise X3DHSignatureError."""
    _require_crypto()
    try:
        _ed25519_pub_from_bytes(identity_sign_pub).verify(
            signature, signed_prekey_pub)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# ── Prekey bundle shape ────────────────────────────────────────────


class PreKeyBundle(NamedTuple):
    """The PUBLIC bundle a responder publishes for initiators to fetch.

    Fields:
      identity_pub:        IK_pub (X25519, 32 bytes)
      identity_sign_pub:   IK_sign_pub (Ed25519, 32 bytes)
      signed_prekey_pub:   SPK_pub (X25519, 32 bytes)
      signed_prekey_sig:   64-byte Ed25519 sig over SPK_pub
      one_time_prekey_pub: OPK_pub (X25519, 32 bytes) — optional
      one_time_prekey_id:  Opaque id used to look up OPK_priv on
                           the responder's side.  Serialized to the
                           PreKeyMessage so responder can find +
                           delete the right OPK.
    """
    identity_pub: bytes
    identity_sign_pub: bytes
    signed_prekey_pub: bytes
    signed_prekey_sig: bytes
    one_time_prekey_pub: Optional[bytes]
    one_time_prekey_id: Optional[str]


class PreKeyMessage(NamedTuple):
    """The handshake envelope an initiator sends to a responder.

    Carries the public values the responder needs to recompute the
    same shared secret (DH symmetry).  The first Double-Ratchet
    envelope rides ALONGSIDE this in the wire format below.

    Fields:
      identity_pub_initiator: IK_pub_A
      ephemeral_pub:          EK_pub_A
      one_time_prekey_id:     OPK id consumed (or None)
      ratchet_envelope_blob:  serialized ratchet envelope (the first
                              encrypted message) — opaque to X3DH
    """
    identity_pub_initiator: bytes
    ephemeral_pub: bytes
    one_time_prekey_id: Optional[str]
    ratchet_envelope_blob: bytes


# ── Initiator path ─────────────────────────────────────────────────


def initiator_derive_shared_secret(
        identity_priv: bytes,
        bundle: PreKeyBundle,
        *,
        ephemeral_priv: Optional[bytes] = None,
        verify_sig: bool = True) -> Tuple[bytes, bytes, bytes]:
    """Run the X3DH handshake from the initiator's side.

    Returns (shared_secret, ephemeral_priv, ephemeral_pub).
    Caller persists the ephemeral pub in the PreKeyMessage and
    feeds shared_secret into init_ratchet().

    `ephemeral_priv` is an optional override (testing seam) — by
    default a fresh ephemeral keypair is generated per session.
    `verify_sig=True` (default) refuses to handshake if the SPK
    signature doesn't verify under the identity_sign_pub; the
    caller can explicitly disable for legacy bundles, but production
    deploys MUST keep it on.
    """
    _require_crypto()
    if verify_sig:
        if not verify_prekey_signature(
                bundle.identity_sign_pub,
                bundle.signed_prekey_pub,
                bundle.signed_prekey_sig):
            raise X3DHSignatureError(
                "SPK signature verification failed — bundle may be "
                "tampered or identity_sign_pub mismatched")

    if ephemeral_priv is None:
        ephemeral_priv, ephemeral_pub = generate_x25519_keypair()
    else:
        ephemeral_pub = _x25519_pub_bytes(
            _x25519_priv_from_bytes(ephemeral_priv))

    # Four DH computations.  Order matches the libsignal X3DH spec
    # so DH1..DH4 are concatenated in a fixed sequence both sides
    # agree on.
    dh1 = _dh(identity_priv,  bundle.signed_prekey_pub)
    dh2 = _dh(ephemeral_priv, bundle.identity_pub)
    dh3 = _dh(ephemeral_priv, bundle.signed_prekey_pub)
    if bundle.one_time_prekey_pub is not None:
        dh4 = _dh(ephemeral_priv, bundle.one_time_prekey_pub)
        ikm = dh1 + dh2 + dh3 + dh4
    else:
        ikm = dh1 + dh2 + dh3
    sk = _x3dh_kdf(ikm)
    return sk, ephemeral_priv, ephemeral_pub


# ── Responder path ─────────────────────────────────────────────────


def responder_derive_shared_secret(
        identity_priv: bytes,
        signed_prekey_priv: bytes,
        prekey_message: PreKeyMessage,
        *,
        one_time_prekey_priv: Optional[bytes] = None) -> bytes:
    """Run the X3DH handshake from the responder's side.

    Caller is responsible for looking up `one_time_prekey_priv` from
    `prekey_message.one_time_prekey_id` (and DELETING it from the
    prekey store before this call returns).  None means the initiator
    didn't consume an OPK.

    Returns the shared_secret matching the initiator's by ECDH
    symmetry.
    """
    _require_crypto()
    dh1 = _dh(signed_prekey_priv, prekey_message.identity_pub_initiator)
    dh2 = _dh(identity_priv,      prekey_message.ephemeral_pub)
    dh3 = _dh(signed_prekey_priv, prekey_message.ephemeral_pub)
    if prekey_message.one_time_prekey_id is not None:
        if one_time_prekey_priv is None:
            raise X3DHError(
                "PreKeyMessage references one_time_prekey_id but "
                "the responder didn't supply the matching priv — "
                "OPK already consumed or unknown id")
        dh4 = _dh(one_time_prekey_priv, prekey_message.ephemeral_pub)
        ikm = dh1 + dh2 + dh3 + dh4
    else:
        ikm = dh1 + dh2 + dh3
    return _x3dh_kdf(ikm)


# ── KDF ────────────────────────────────────────────────────────────


def _x3dh_kdf(ikm: bytes) -> bytes:
    """X3DH-spec KDF: HKDF-SHA256 with a fixed 32-byte 0xff salt
    prefix prepended to the input keying material, returning a
    32-byte shared_secret.

    The prefix is a domain-separation guard the spec recommends so
    a zero-IKM attacker can't forge a known shared_secret."""
    _require_crypto()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=SHARED_SECRET_BYTES,
        salt=b'\x00' * 32,
        info=HKDF_INFO,
    ).derive(HKDF_SALT_PREFIX + ikm)


# ── PreKeyMessage wire format ──────────────────────────────────────


def serialize_prekey_message(msg: PreKeyMessage) -> bytes:
    """Compact wire format:
      [u8 version=1]
      [u8 opk_present (0 or 1)]
      [u8 opk_id_len][opk_id_bytes...]   — only if opk_present
      [u8 ik_len=32][ik_pub...]
      [u8 ek_len=32][ek_pub...]
      [u32 ratchet_len][ratchet_envelope_blob...]
    """
    _require_crypto()
    out = bytearray()
    out.append(1)  # version
    if msg.one_time_prekey_id is not None:
        out.append(1)
        opk_id = msg.one_time_prekey_id.encode('utf-8')
        if len(opk_id) > 255:
            raise X3DHError(
                f"one_time_prekey_id too long ({len(opk_id)} > 255)")
        out.append(len(opk_id))
        out += opk_id
    else:
        out.append(0)
    out.append(len(msg.identity_pub_initiator))
    out += msg.identity_pub_initiator
    out.append(len(msg.ephemeral_pub))
    out += msg.ephemeral_pub
    out += struct.pack('>I', len(msg.ratchet_envelope_blob))
    out += msg.ratchet_envelope_blob
    return bytes(out)


def deserialize_prekey_message(buf: bytes) -> PreKeyMessage:
    if len(buf) < 1 + 1 + 1 + 32 + 1 + 32 + 4:
        raise X3DHError("PreKeyMessage too short")
    p = 0
    version = buf[p]; p += 1
    if version != 1:
        raise X3DHError(f"unknown PreKeyMessage version: {version}")
    opk_present = buf[p]; p += 1
    if opk_present:
        opk_id_len = buf[p]; p += 1
        opk_id = buf[p:p + opk_id_len].decode('utf-8'); p += opk_id_len
    else:
        opk_id = None
    ik_len = buf[p]; p += 1
    ik = bytes(buf[p:p + ik_len]); p += ik_len
    ek_len = buf[p]; p += 1
    ek = bytes(buf[p:p + ek_len]); p += ek_len
    rk_len = struct.unpack('>I', buf[p:p + 4])[0]; p += 4
    rk = bytes(buf[p:p + rk_len])
    return PreKeyMessage(
        identity_pub_initiator=ik,
        ephemeral_pub=ek,
        one_time_prekey_id=opk_id,
        ratchet_envelope_blob=rk,
    )


__all__ = [
    'X3DHError', 'X3DHUnavailable', 'X3DHSignatureError',
    'PreKeyBundle', 'PreKeyMessage',
    'generate_x25519_keypair', 'generate_ed25519_keypair',
    'sign_prekey', 'verify_prekey_signature',
    'initiator_derive_shared_secret',
    'responder_derive_shared_secret',
    'serialize_prekey_message', 'deserialize_prekey_message',
    'SHARED_SECRET_BYTES', 'HKDF_INFO',
]
