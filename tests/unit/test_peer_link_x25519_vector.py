"""The desktop and a phone derive the SAME PeerLink session key.

A fixed-key vector, asserted here against the desktop's code and in the phone
repo against the phone's (android .../peerlink/PeerLinkCryptoX25519InteropTest.kt).
Before 2026-10-06 the phone sent its X25519 key X.509-wrapped (44 bytes), read
the desktop's raw 32-byte key as an X.509 encoding (which it is not), and below
Android 12 made a P-256 key instead.  The relay link (core.peer_link.relay) is
never plaintext, so a phone reaches its desktop there only if both ends derive
this same key.
"""
import os
import sys

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import link as link_mod  # noqa: E402

PHONE_PRIVATE = bytes(range(1, 33))
DESKTOP_PRIVATE = bytes(range(33, 65))
PHONE_PUBLIC = '07a37cbc142093c8b755dc1b10e86cb426374ad16aa853ed0bdfc0b2b86d1c7c'
DESKTOP_PUBLIC = '5869aff450549732cbaaed5e5df9b30a6da31cb0e5742bad5ad4a1a768f1a67b'
SESSION_KEY = 'ce5fe353b43dad6ded81223dfa28ca993fde7923ae8bca952bc76f7a72c5ec74'
SPKI_PREFIX = '302a300506032b656e032100'


def test_the_desktop_derives_the_key_the_phone_derives():
    desk = X25519PrivateKey.from_private_bytes(DESKTOP_PRIVATE)
    assert link_mod.session_key_from(desk, PHONE_PUBLIC).hex() == SESSION_KEY


def test_the_key_is_symmetric():
    phone = X25519PrivateKey.from_private_bytes(PHONE_PRIVATE)
    assert link_mod.session_key_from(phone, DESKTOP_PUBLIC).hex() == SESSION_KEY


def test_a_wrapped_phone_key_gives_the_same_session_key():
    desk = X25519PrivateKey.from_private_bytes(DESKTOP_PRIVATE)
    assert link_mod.session_key_from(desk, SPKI_PREFIX + PHONE_PUBLIC).hex() == SESSION_KEY
