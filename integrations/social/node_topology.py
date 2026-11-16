"""Whether this node is flat: ONE rule, as claimed AND as proven.

A flat node is one person's machine (a desktop).  Two rules ask it: who
owns the machine, for a call grant (call_service._machine_owner), and
whether this node's own experiments evolve here with no vote (voting_rules.
evolves_without_vote_here, owner ruling 2026-11-15).  The second used to
read the claim alone, beside the first's claim-and-proof, so a node its
master key made central with no claim set counted as flat for votes and not
for calls (review rv_9dd0 of 9dd0d25d7, F4).  Both ask this now.

Not in security/key_delegation.py beside get_node_tier: that file is one of
the guardrails' PROTECTED_FILES.
"""
import os


def is_flat_node() -> bool:
    """True only when this node is flat both as CLAIMED and as PROVEN.

    The claim (HEVOLVE_NODE_TIER; unset or empty reads 'flat', as
    key_delegation.get_node_tier reads it) withholds flat: the product's own
    promotions set 'regional' with no certificate and leave an upgraded
    central with no key, and get_node_tier, which asks what a node has
    PROVEN, answers 'flat' for both.  The proof catches the converse, a node
    promoted to central by its key with no claim set.  Any claim but 'flat'
    -- 'local', a value no rule knows -- is not flat.  get_node_tier is asked
    only when the claim is flat."""
    claimed = (os.environ.get('HEVOLVE_NODE_TIER') or 'flat').strip().lower()
    if claimed != 'flat':
        return False
    from security.key_delegation import get_node_tier
    return get_node_tier() == 'flat'
