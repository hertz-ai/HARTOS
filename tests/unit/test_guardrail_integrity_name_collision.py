"""Parallel-path fix: two DIFFERENT functions were both named
``verify_guardrail_integrity`` —

  * ``security.hive_guardrails`` — hash-chain integrity check, returns ``bool``
  * ``security.origin_attestation`` — brand-marker check, returns ``(bool, str)``

Different return types under one name: importing the wrong one broke
``ok, msg = verify_guardrail_integrity()`` unpacking, or made
``if verify_guardrail_integrity():`` always truthy on the tuple. The
origin_attestation one is renamed to ``verify_guardrail_brand_integrity``.
"""


def test_origin_attestation_renamed_and_collision_gone():
    from security import origin_attestation
    assert hasattr(origin_attestation, 'verify_guardrail_brand_integrity'), \
        "renamed brand-integrity accessor must exist"
    assert not hasattr(origin_attestation, 'verify_guardrail_integrity'), \
        "the colliding name must be gone from origin_attestation"


def test_hive_guardrails_keeps_its_name():
    from security import hive_guardrails
    assert hasattr(hive_guardrails, 'verify_guardrail_integrity'), \
        "the hash-chain accessor keeps its canonical name"
