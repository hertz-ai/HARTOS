"""Parallel-path fix #6 (foundation): the Ed25519 canonical-JSON serialization
was re-implemented inline in ``node_integrity`` + several security modules, each
with its own exclude-set. ANY drift in that serialization = signatures silently
fail network-wide. This introduces ONE ``canonical_payload()`` and routes
``node_integrity``'s own sign/verify through it, **byte-identically**.
"""
import json
from security import node_integrity as ni


def _old_serialize(payload, exclude_key='signature'):
    clean = {k: v for k, v in payload.items() if k != exclude_key}
    return json.dumps(clean, sort_keys=True, separators=(',', ':')).encode('utf-8')


SAMPLE = {'b': 2, 'a': 1, 'nested': {'z': [3, 2, 1]}, 'signature': 'deadbeef', 'txt': 'héllo'}


def test_canonical_payload_is_byte_identical_to_old_inline():
    # THE safety guarantee: the serialization must not change or signatures break.
    assert ni.canonical_payload(SAMPLE, exclude=('signature',)) == _old_serialize(SAMPLE)


def test_exclude_is_parameterized_for_other_key_names():
    p = {'x': 1, 'sig': 'abc'}
    assert ni.canonical_payload(p, exclude=('sig',)) == _old_serialize(p, 'sig')
    # a bare str exclude is normalized to a single-key tuple
    assert ni.canonical_payload(p, exclude='sig') == _old_serialize(p, 'sig')


def test_sign_verify_round_trip_with_ephemeral_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    msg = ni.canonical_payload({'a': 1, 'z': 'q', 'signature': 'ignored'})
    sig = priv.sign(msg)
    assert ni.verify_signature(pub_hex, msg, sig) is True
    assert ni.verify_signature(pub_hex, msg + b'tamper', sig) is False
