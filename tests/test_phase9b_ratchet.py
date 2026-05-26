"""Phase 9.B — Double Ratchet for E2E DMs (#267).

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Coverage of the cryptographic invariants that matter for chat:
  - Round-trip: Alice encrypts → Bob decrypts identical plaintext.
  - DH ratchet step: rotating Bob's DH key advances the root chain
    so a compromise of the OLD root key can't decrypt new messages
    (post-compromise security).
  - Out-of-order delivery: Bob can decrypt msg #2 then msg #1 if
    they arrive swapped (skipped-message-key cache).
  - Replay rejection: re-submitting the same envelope raises.
  - Budget cap: a malicious peer who jumps to idx=10000 doesn't OOM
    us via the skipped-message-key cache.
  - Wire format round-trip: serialize → deserialize is identity.
  - AAD binding: an attacker who swaps `our_pub` between conversations
    breaks decryption (AES-GCM tag fails).

Skipped when `cryptography` isn't installed (flat / Nunba bundled
deploys without the dep).
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from integrations.social import e2e_ratchet as r

needs_crypto = pytest.mark.skipif(
    not r._HAS_CRYPTO,
    reason="cryptography package not installed; pip install cryptography")


def _establish_pair():
    """Helper: bootstrap Alice + Bob with a shared secret + initial
    keypairs so they're ready to talk.  Returns (alice, bob)."""
    shared = b'\x42' * 32  # out-of-band; Phase 9.C X3DH replaces this
    a_priv, a_pub = r.generate_dh_keypair()
    b_priv, b_pub = r.generate_dh_keypair()
    # Bob's keypair is known to Alice on init (via the prekey bundle).
    alice = r.init_ratchet(
        shared_secret=shared,
        our_dh_priv=a_priv, our_dh_pub=a_pub,
        their_dh_pub=b_pub)
    # Bob doesn't know Alice's pubkey until the first envelope arrives.
    bob = r.init_ratchet(
        shared_secret=shared,
        our_dh_priv=b_priv, our_dh_pub=b_pub,
        their_dh_pub=None)
    return alice, bob


@needs_crypto
def test_round_trip_basic():
    alice, bob = _establish_pair()
    plaintext = b"hello bob, it's me"
    alice2, env = r.encrypt_message(alice, plaintext)
    bob2, decrypted = r.decrypt_message(bob, env)
    assert decrypted == plaintext


@needs_crypto
def test_multiple_messages_advance_chain():
    """Three messages on the same chain — each gets a unique key."""
    alice, bob = _establish_pair()
    msgs = [b"first", b"second", b"third"]
    envelopes = []
    for m in msgs:
        alice, env = r.encrypt_message(alice, m)
        envelopes.append(env)
    for m, env in zip(msgs, envelopes):
        bob, dec = r.decrypt_message(bob, env)
        assert dec == m


@needs_crypto
def test_out_of_order_delivery():
    """Bob receives msg2 first, then msg1.  Both decrypt because
    the skipped-message-key cache holds msg1's key."""
    alice, bob = _establish_pair()
    alice, e1 = r.encrypt_message(alice, b"first")
    alice, e2 = r.encrypt_message(alice, b"second")
    # Out of order: e2 arrives first
    bob, d2 = r.decrypt_message(bob, e2)
    assert d2 == b"second"
    bob, d1 = r.decrypt_message(bob, e1)
    assert d1 == b"first"


@needs_crypto
def test_replay_rejected():
    """Same envelope twice → second decrypt raises."""
    alice, bob = _establish_pair()
    alice, env = r.encrypt_message(alice, b"replay me")
    bob, _ = r.decrypt_message(bob, env)
    with pytest.raises((r.RatchetReplayError, Exception)):
        # Either the explicit replay error, or the AES-GCM tag fail
        # if the chain has advanced past idx.
        r.decrypt_message(bob, env)


@needs_crypto
def test_skipped_message_budget_cap():
    """A peer claiming idx=MAX+1 raises instead of OOM-caching."""
    alice, bob = _establish_pair()
    # Forge an envelope at impossible idx
    forged = {
        'version': r.WIRE_VERSION,
        'our_pub': alice.our_dh_pub,
        'idx': r.MAX_SKIPPED_MESSAGES + 5,
        'nonce': b'\x00' * 12,
        'ciphertext': b'\x00' * 48,
    }
    with pytest.raises(r.RatchetSkippedMessageBudgetExceeded):
        r.decrypt_message(bob, forged)


@needs_crypto
def test_dh_ratchet_advances_root():
    """After Alice rotates her DH key, Bob's root advances on receive,
    so a snapshot of the old root key can't decrypt the new chain."""
    alice, bob = _establish_pair()
    # First message — establishes Bob's view of Alice's pubkey.
    alice, e1 = r.encrypt_message(alice, b"before rotation")
    bob, _ = r.decrypt_message(bob, e1)
    old_root = bob.root_key
    # Alice rotates DH (in real flow this happens when Bob's first
    # reply arrives; here we manually advance Alice for the test).
    new_priv, new_pub = r.generate_dh_keypair()
    # Replace Alice's keypair + step her root (mirrors what
    # advance_dh_ratchet does).  Pure-test setup.
    alice = r.advance_dh_ratchet(alice, bob.our_dh_pub)
    alice, e2 = r.encrypt_message(alice, b"after rotation")
    bob, dec = r.decrypt_message(bob, e2)
    assert dec == b"after rotation"
    assert bob.root_key != old_root, (
        "DH ratchet step must advance the root key; otherwise old-key "
        "compromise breaks post-compromise security")


@needs_crypto
def test_wire_serialize_round_trip():
    alice, _ = _establish_pair()
    alice, env = r.encrypt_message(alice, b"wire test")
    blob = r.serialize_envelope(env)
    parsed = r.deserialize_envelope(blob)
    assert parsed['version'] == env['version']
    assert parsed['our_pub'] == env['our_pub']
    assert parsed['idx'] == env['idx']
    assert parsed['nonce'] == env['nonce']
    assert parsed['ciphertext'] == env['ciphertext']


@needs_crypto
def test_aad_binding_prevents_swap_attack():
    """An attacker can't take an envelope from one conversation and
    relay it as if it were from another peer — AES-GCM AAD binding
    on (sender_pub, receiver_pub, idx) makes the tag fail."""
    alice, bob = _establish_pair()
    alice, env = r.encrypt_message(alice, b"private to bob")
    # Swap our_pub to a fake key — the AAD won't match, GCM tag fails.
    fake_priv, fake_pub = r.generate_dh_keypair()
    tampered = dict(env)
    tampered['our_pub'] = fake_pub
    with pytest.raises(Exception):
        r.decrypt_message(bob, tampered)


@needs_crypto
def test_init_ratchet_rejects_short_shared_secret():
    a_priv, a_pub = r.generate_dh_keypair()
    with pytest.raises(r.RatchetError):
        r.init_ratchet(
            shared_secret=b'\x00' * 8,  # too short
            our_dh_priv=a_priv, our_dh_pub=a_pub,
            their_dh_pub=None)


def test_unavailable_when_no_crypto(monkeypatch):
    """Doc invariant: when `cryptography` is unavailable, every
    primitive raises RatchetUnavailable.  We can't actually
    uninstall cryptography mid-test, so we monkeypatch the flag."""
    monkeypatch.setattr(r, '_HAS_CRYPTO', False)
    with pytest.raises(r.RatchetUnavailable):
        r._require_crypto()


def test_module_imports_cleanly():
    """A bare deploy without `cryptography` must still import this
    module — only the operations raise.  Already implicitly tested
    by every import in this file, but locked here explicitly."""
    from integrations.social import e2e_ratchet  # noqa: F401
    assert hasattr(e2e_ratchet, 'init_ratchet')
    assert hasattr(e2e_ratchet, 'encrypt_message')
