"""Parallel-path fix #7: two different ``get_node_identity()`` functions existed —
the CRYPTO identity (``security.node_integrity`` → node_id / user_id / pubkey)
and the ONBOARDING identity (``hart_onboarding`` → node_tag / tier / language).
Both returned a dict carrying ``tier``, a name-collision footgun where importing
the wrong module silently returned the wrong dict. The onboarding one is renamed
to ``get_onboarding_identity``.
"""
from hartos import hart_onboarding


def test_onboarding_identity_renamed_and_collision_gone():
    assert hasattr(hart_onboarding, 'get_onboarding_identity'), \
        "renamed onboarding accessor must exist"
    assert not hasattr(hart_onboarding, 'get_node_identity'), \
        "the colliding name must be gone from hart_onboarding"


def test_crypto_identity_keeps_its_name():
    from security import node_integrity
    assert hasattr(node_integrity, 'get_node_identity'), \
        "the crypto identity accessor is unchanged"
