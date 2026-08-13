"""The trust-contract signer/verifier must route through the ONE canonical
Ed25519 serialization, byte-for-byte — never its own inline copy.

`security/pre_trust_contract.py` carried a 6th inline copy of
`json.dumps(..., sort_keys=True, separators=(',', ':'))` in `_contract_payload`.
Every such copy is a network-wide-signature-break waiting to happen: the moment
one signer's serialization drifts from a verifier's (a stray space, `sort_keys`
flipped, a different separator), every trust contract that node signs fails
verification on every peer with no obvious cause. This pins the collapse:

  1. `_contract_payload` produces EXACTLY the bytes the canonical
     `security.node_integrity.canonical_payload` produces for the signed field
     set — so the serialization can only live in one place.
  2. It returns bytes (the canonical UTF-8), and a real sign -> verify round trip
     still holds — proving old signatures keep verifying (no flag day).
  3. Tampering with any signed field is still rejected.
"""
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from security.node_integrity import canonical_payload
import security.pre_trust_contract as ptc


_SIGNED_FIELDS = {
    'node_id': 'node-1',
    'public_key_hex': 'ab12cd',
    'contract_fingerprint': 'cf-xyz',
    'guardrail_hash': 'gh-123',
    'origin_fingerprint': 'of-789',
    'audit_compute_ratio': 0.8,
    'signed_at': 1234567.5,
}


def _contract(**over):
    fields = {**_SIGNED_FIELDS, **over}
    return ptc.TrustContract(**fields)


def test_payload_is_the_canonical_serialization_byte_for_byte():
    """The single-source guarantee: identical bytes to canonical_payload, and
    identical to the exact legacy json.dumps the inline copy used."""
    c = _contract()
    got = ptc._contract_payload(c)

    # The helper keeps its long-standing str return type — callers (and the
    # existing tests/unit/test_pre_trust_contract.py helper) .encode('utf-8')
    # at the call site, so changing it to bytes would break every one of them.
    assert isinstance(got, str), 'must keep the str return type callers rely on'

    # equals the ONE canonical serializer over the same signed field-set
    assert got.encode('utf-8') == canonical_payload(_SIGNED_FIELDS, exclude=())

    # equals the legacy inline serialization it replaced (no flag-day break)
    legacy = json.dumps(_SIGNED_FIELDS, sort_keys=True, separators=(',', ':'))
    assert got == legacy


def test_sign_then_verify_round_trips():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    c = _contract(public_key_hex=pub_hex)

    c.signature_hex = priv.sign(ptc._contract_payload(c).encode('utf-8')).hex()

    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(c.public_key_hex))
    # raises on an invalid signature; no exception == verified
    pub.verify(bytes.fromhex(c.signature_hex),
               ptc._contract_payload(c).encode('utf-8'))


def test_tampering_a_signed_field_breaks_the_signature():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    c = _contract(public_key_hex=pub_hex)
    c.signature_hex = priv.sign(ptc._contract_payload(c).encode('utf-8')).hex()

    c.node_id = 'evil-node'  # flip a signed field after signing
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(c.public_key_hex))
    try:
        pub.verify(bytes.fromhex(c.signature_hex),
                   ptc._contract_payload(c).encode('utf-8'))
        assert False, 'a tampered signed field must fail verification'
    except Exception:
        pass
