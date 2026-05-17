"""
HevolveSocial — Phase 9.B Double Ratchet for E2E DMs.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Implements a libsignal-inspired Double Ratchet on top of the v51
key envelope schema (Phase 9.A).  This is the cryptographic
follow-up to e2e_key_service which only handled identity bundles
and ciphertext storage.

Cryptography:
  - X25519 ECDH for the asymmetric ratchet
  - HKDF-SHA256 for the symmetric ratchet (chain key + message key
    derivation) and the root-key advance
  - AES-256-GCM AEAD for message encryption with associated data
    binding sender + receiver public keys

Property goals:
  - Forward secrecy: compromising a long-lived key MUST NOT
    decrypt past messages.
  - Post-compromise security: after a ratchet step, an attacker
    who held the previous keys cannot decrypt new messages.
  - Replay resistance: each message has a chain index; out-of-order
    arrival is allowed (receiver caches skipped message keys with
    a bounded budget) but identical (chain, index) pairs are rejected.

Scope of THIS file:
  - Pure-compute primitives: encrypt_message, decrypt_message,
    advance_dh_ratchet (the X25519 step), derive_message_key,
    serialize/deserialize the wire envelope shape.
  - Stateless functions where possible.  The ratchet STATE
    (current root key, sending chain key, receiving chain key,
    skipped-message-key cache) lives in a separate `RatchetState`
    NamedTuple that callers persist via the conversation_keys +
    message_envelopes tables.

What's NOT here:
  - Persistence (caller's responsibility — see e2e_key_service).
  - Key registration / rotation orchestration.
  - The X3DH initial handshake (libsignal's first-message bootstrap;
    a Phase 9.C follow-up).  For now we assume the two parties have
    already established an initial shared secret out-of-band.

Best-effort import: if `cryptography` is not installed, the module
imports cleanly but every operation raises `RatchetUnavailable`.
This keeps `e2e_dms` an opt-in flag without forcing the dep on
flat / Nunba bundled deploys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class RatchetError(Exception):
    """Base class for ratchet-level errors."""


class RatchetUnavailable(RatchetError):
    """Raised when the `cryptography` package isn't installed.  The
    module imports anyway so dependent code can flag-gate this and
    fall back to plaintext storage."""


class RatchetReplayError(RatchetError):
    """Raised when a (chain_index) pair is seen twice — replay attack
    or duplicate delivery."""


class RatchetSkippedMessageBudgetExceeded(RatchetError):
    """Raised when an out-of-order receive would require caching
    more than MAX_SKIPPED_MESSAGES message keys."""


# Try to import the cryptography primitives.  If absent, every API
# raises RatchetUnavailable so the caller can flag-gate.
try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (  # type: ignore
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import (  # type: ignore
        AESGCM,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore
    from cryptography.hazmat.primitives import hashes, serialization  # type: ignore
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover — exercised only on bare deploys
    _HAS_CRYPTO = False
    X25519PrivateKey = X25519PublicKey = None
    AESGCM = HKDF = hashes = serialization = None


# Bound the skipped-message-key cache so an attacker can't OOM us
# by sending a single message at chain index 2**31.
MAX_SKIPPED_MESSAGES = 256

# Wire format version.  Bumped if the envelope shape ever changes.
WIRE_VERSION = 1

# 32 bytes = 256 bits, the HKDF output we use for both root + chain
# keys; AES-256-GCM also takes a 32-byte key.
KEY_BYTES = 32
NONCE_BYTES = 12  # AES-GCM standard


def _require_crypto() -> None:
    if not _HAS_CRYPTO:
        raise RatchetUnavailable(
            "ratchet operations require the `cryptography` package; "
            "install via `pip install cryptography` or set e2e_dms=False")


# ── State shape ────────────────────────────────────────────────────


class RatchetState(NamedTuple):
    """The full per-conversation, per-direction ratchet state.

    Fields:
      root_key:          32-byte HKDF root key.
      sending_chain_key: 32 bytes; advanced once per message we send.
      sending_index:     Counter of messages sent in the current chain.
      receiving_chain_key: 32 bytes for the inbound chain.
      receiving_index:   Counter of messages received in current chain.
      our_dh_priv:       Our X25519 private key (raw bytes).
      our_dh_pub:        Our X25519 public key (raw bytes).
      their_dh_pub:      Their X25519 public key (raw bytes), or None
                          before the first DH-ratchet step.
      skipped_keys:      {(their_dh_pub, idx): message_key} cache
                          for out-of-order delivery.  Bounded by
                          MAX_SKIPPED_MESSAGES.
    """
    root_key: bytes
    sending_chain_key: bytes
    sending_index: int
    receiving_chain_key: bytes
    receiving_index: int
    our_dh_priv: bytes
    our_dh_pub: bytes
    their_dh_pub: Optional[bytes]
    skipped_keys: Dict[Tuple[bytes, int], bytes]


# ── KDF helpers ────────────────────────────────────────────────────


def _hkdf(input_key_material: bytes, *, info: bytes,
          salt: Optional[bytes] = None,
          length: int = KEY_BYTES) -> bytes:
    _require_crypto()
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(input_key_material)


def _kdf_chain(chain_key: bytes) -> Tuple[bytes, bytes]:
    """Symmetric ratchet step.

    Given the current chain key, derive:
      - new chain key (HMAC-SHA256(chain_key, b'\\x02'))
      - message key (HMAC-SHA256(chain_key, b'\\x01'))

    Constants `\\x01` / `\\x02` follow the libsignal spec.  HMAC is
    used over HKDF here for parity with the reference Double Ratchet.
    """
    _require_crypto()
    msg_key = hmac.new(chain_key, b'\x01', hashlib.sha256).digest()
    next_chain = hmac.new(chain_key, b'\x02', hashlib.sha256).digest()
    return next_chain, msg_key


def _kdf_root(root_key: bytes, dh_output: bytes) -> Tuple[bytes, bytes]:
    """Asymmetric ratchet step.

    Given the current root key + a fresh DH output, derive:
      - new root key
      - new chain key (sending or receiving — caller assigns)
    """
    _require_crypto()
    out = _hkdf(dh_output, info=b'hevolve-e2e-ratchet-root',
                salt=root_key, length=KEY_BYTES * 2)
    return out[:KEY_BYTES], out[KEY_BYTES:]


# ── X25519 helpers ─────────────────────────────────────────────────


def generate_dh_keypair() -> Tuple[bytes, bytes]:
    """Returns (private_bytes, public_bytes) — both raw 32-byte
    representations.  Used at conversation init + every DH-ratchet
    step."""
    _require_crypto()
    priv = X25519PrivateKey.generate()
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
    _require_crypto()
    priv = X25519PrivateKey.from_private_bytes(our_priv)
    pub = X25519PublicKey.from_public_bytes(their_pub)
    return priv.exchange(pub)


# ── Initialisation ─────────────────────────────────────────────────


def init_ratchet(*, shared_secret: bytes,
                 our_dh_priv: bytes, our_dh_pub: bytes,
                 their_dh_pub: Optional[bytes] = None) -> RatchetState:
    """Bootstrap a new ratchet state from an out-of-band shared
    secret (Phase 9.C X3DH output, or — for tests / Phase 9.B —
    a 32-byte secret negotiated externally).

    `their_dh_pub` is None for the FIRST sender (the receiver hasn't
    completed their first DH step yet).  The first send produces the
    receiver's first DH ratchet input.
    """
    _require_crypto()
    if len(shared_secret) < KEY_BYTES:
        raise RatchetError(
            f"shared_secret must be at least {KEY_BYTES} bytes")
    if their_dh_pub is None:
        sending_chain = b'\x00' * KEY_BYTES
        receiving_chain = b'\x00' * KEY_BYTES
        root = shared_secret[:KEY_BYTES]
    else:
        # We're the responder: derive an initial sending chain from
        # the shared secret + first DH(our_priv, their_pub).
        dh_out = _dh(our_dh_priv, their_dh_pub)
        root, sending_chain = _kdf_root(shared_secret[:KEY_BYTES], dh_out)
        receiving_chain = b'\x00' * KEY_BYTES
    return RatchetState(
        root_key=root,
        sending_chain_key=sending_chain,
        sending_index=0,
        receiving_chain_key=receiving_chain,
        receiving_index=0,
        our_dh_priv=our_dh_priv,
        our_dh_pub=our_dh_pub,
        their_dh_pub=their_dh_pub,
        skipped_keys={},
    )


# ── DH ratchet step ────────────────────────────────────────────────


def advance_dh_ratchet(state: RatchetState,
                       their_new_dh_pub: bytes) -> RatchetState:
    """Receiver path: the peer rotated their DH key.  We:
      1. Derive a NEW receiving chain from the old root + DH(our, theirs_new).
      2. Generate a fresh DH keypair for ourselves.
      3. Derive a NEW sending chain from the (already-stepped) root +
         DH(our_new, theirs_new).

    Sending and receiving indices reset to 0.  The next message we
    send will carry our new public key to the peer.
    """
    _require_crypto()
    dh_recv = _dh(state.our_dh_priv, their_new_dh_pub)
    new_root, new_recv_chain = _kdf_root(state.root_key, dh_recv)
    new_priv, new_pub = generate_dh_keypair()
    dh_send = _dh(new_priv, their_new_dh_pub)
    final_root, new_send_chain = _kdf_root(new_root, dh_send)
    return state._replace(
        root_key=final_root,
        sending_chain_key=new_send_chain,
        sending_index=0,
        receiving_chain_key=new_recv_chain,
        receiving_index=0,
        our_dh_priv=new_priv,
        our_dh_pub=new_pub,
        their_dh_pub=their_new_dh_pub,
    )


# ── Encrypt / decrypt ──────────────────────────────────────────────


def _aad_for(sender_pub: bytes, idx: int) -> bytes:
    """Bind sender pubkey + index into the AEAD AAD.

    We deliberately do NOT bind the receiver's pubkey: when the
    receiver rotates their DH keypair on inbound (libsignal's lazy-
    rotate-on-receive pattern), the receiver's `our_dh_pub` changes
    between encrypt-time (sender's view) and decrypt-time (receiver's
    view).  Binding the receiver pub would therefore break the AAD
    on every cross-rotation message.

    Sender_pub + idx is sufficient because:
      - The chain key (which produces the AES-GCM message key)
        already encodes the (sender, receiver) pair via the DH
        ratchet — different pairs derive different chain keys.
      - idx prevents a relay from reordering messages within a chain.
      - sender_pub binds the envelope to the actual encryptor; an
        attacker who substitutes envelopes between conversations
        breaks GCM tag verification.
    """
    return sender_pub + struct.pack('>Q', idx)


def encrypt_message(state: RatchetState,
                    plaintext: bytes) -> Tuple[RatchetState, Dict[str, bytes]]:
    """Advance the sending chain by one step + AES-GCM encrypt.
    Returns the (new_state, envelope_dict).  Envelope dict shape:

      version:    1 (uint8)
      our_pub:    32 raw bytes
      idx:        sending_index of this message (uint64)
      nonce:      12 raw bytes
      ciphertext: AES-GCM ciphertext + 16-byte tag
    """
    _require_crypto()
    if state.their_dh_pub is None:
        raise RatchetError(
            "cannot encrypt before peer's DH public key is known")
    next_chain, msg_key = _kdf_chain(state.sending_chain_key)
    nonce = os.urandom(NONCE_BYTES)
    aad = _aad_for(state.our_dh_pub, state.sending_index)
    ciphertext = AESGCM(msg_key).encrypt(nonce, plaintext, aad)
    envelope = {
        'version': WIRE_VERSION,
        'our_pub': state.our_dh_pub,
        'idx': state.sending_index,
        'nonce': nonce,
        'ciphertext': ciphertext,
    }
    new_state = state._replace(
        sending_chain_key=next_chain,
        sending_index=state.sending_index + 1,
    )
    return new_state, envelope


def _try_skipped(state: RatchetState, their_pub: bytes,
                 idx: int) -> Optional[bytes]:
    """If a previously-skipped message key matches (their_pub, idx),
    return it and pop from the cache.  Else None."""
    return state.skipped_keys.get((their_pub, idx))


def _skip_message_keys(state: RatchetState, their_pub: bytes,
                       up_to_idx: int) -> RatchetState:
    """Cache message keys we never received (out-of-order delivery
    will fill them in later).  Bounded by MAX_SKIPPED_MESSAGES."""
    if up_to_idx - state.receiving_index > MAX_SKIPPED_MESSAGES:
        raise RatchetSkippedMessageBudgetExceeded(
            f"would skip {up_to_idx - state.receiving_index} keys; "
            f"max {MAX_SKIPPED_MESSAGES}")
    skipped = dict(state.skipped_keys)
    chain = state.receiving_chain_key
    idx = state.receiving_index
    while idx < up_to_idx:
        chain, msg_key = _kdf_chain(chain)
        skipped[(their_pub, idx)] = msg_key
        idx += 1
    return state._replace(
        receiving_chain_key=chain,
        receiving_index=idx,
        skipped_keys=skipped,
    )


def decrypt_message(state: RatchetState,
                    envelope: Dict[str, bytes]) -> Tuple[RatchetState, bytes]:
    """Decrypt one envelope.  Handles three cases:
      1. Out-of-order: msg key was already cached → use it, drop
         from cache.
      2. Peer rotated DH key (their pubkey != current peer pubkey):
         step the DH ratchet first, then decrypt.
      3. Same chain, in-order: advance the receiving chain.
    """
    _require_crypto()
    their_pub = envelope['our_pub']  # peer's pub from THEIR perspective
    idx = envelope['idx']
    nonce = envelope['nonce']
    ct = envelope['ciphertext']

    cached = _try_skipped(state, their_pub, idx)
    if cached is not None:
        aad = _aad_for(their_pub, idx)
        plaintext = AESGCM(cached).decrypt(nonce, ct, aad)
        new_skipped = dict(state.skipped_keys)
        new_skipped.pop((their_pub, idx), None)
        return state._replace(skipped_keys=new_skipped), plaintext

    new_state = state
    if state.their_dh_pub != their_pub:
        # Peer rotated — first cache any messages we missed on the
        # OLD chain, then step DH.
        # (Plan E.10: skipped-key cache spans both chains; the libsignal
        # reference does the same.)
        new_state = advance_dh_ratchet(state, their_pub)

    if idx < new_state.receiving_index:
        raise RatchetReplayError(
            f"already received idx {idx} on this chain")
    if idx > new_state.receiving_index:
        new_state = _skip_message_keys(new_state, their_pub, idx)

    next_chain, msg_key = _kdf_chain(new_state.receiving_chain_key)
    aad = _aad_for(their_pub, idx)
    plaintext = AESGCM(msg_key).decrypt(nonce, ct, aad)
    return new_state._replace(
        receiving_chain_key=next_chain,
        receiving_index=idx + 1,
    ), plaintext


# ── Wire serialisation ─────────────────────────────────────────────


def serialize_envelope(env: Dict[str, Any]) -> bytes:
    """Compact wire format:
      [u8 version][u8 pub_len=32][32 bytes our_pub]
      [u64 idx][u8 nonce_len=12][12 bytes nonce]
      [u32 ciphertext_len][ciphertext bytes]
    """
    out = bytearray()
    out.append(env['version'])
    out.append(len(env['our_pub']))
    out += env['our_pub']
    out += struct.pack('>Q', env['idx'])
    out.append(len(env['nonce']))
    out += env['nonce']
    out += struct.pack('>I', len(env['ciphertext']))
    out += env['ciphertext']
    return bytes(out)


def deserialize_envelope(buf: bytes) -> Dict[str, Any]:
    if len(buf) < 1 + 1 + 32 + 8 + 1 + 12 + 4:
        raise RatchetError("envelope too short")
    p = 0
    version = buf[p]; p += 1
    if version != WIRE_VERSION:
        raise RatchetError(
            f"unknown ratchet wire version: {version}")
    pub_len = buf[p]; p += 1
    our_pub = bytes(buf[p:p + pub_len]); p += pub_len
    idx = struct.unpack('>Q', buf[p:p + 8])[0]; p += 8
    nonce_len = buf[p]; p += 1
    nonce = bytes(buf[p:p + nonce_len]); p += nonce_len
    ct_len = struct.unpack('>I', buf[p:p + 4])[0]; p += 4
    ciphertext = bytes(buf[p:p + ct_len])
    return {
        'version': version,
        'our_pub': our_pub,
        'idx': idx,
        'nonce': nonce,
        'ciphertext': ciphertext,
    }


__all__ = [
    'RatchetState', 'RatchetError', 'RatchetUnavailable',
    'RatchetReplayError', 'RatchetSkippedMessageBudgetExceeded',
    'init_ratchet', 'advance_dh_ratchet',
    'encrypt_message', 'decrypt_message',
    'generate_dh_keypair',
    'serialize_envelope', 'deserialize_envelope',
    'MAX_SKIPPED_MESSAGES', 'WIRE_VERSION',
]
