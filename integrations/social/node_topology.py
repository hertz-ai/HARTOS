"""Whether this node is flat: ONE rule, as claimed AND as proven.

A flat node is one person's machine (a desktop).  Two rules ask it: who
owns the machine, for a call grant (call_service._machine_owner), and
whether this node's own experiments evolve here with no vote (voting_rules.
evolves_without_vote_here, owner ruling 2026-11-15).  The second used to
read the claim alone, beside the first's claim-and-proof, so a node its
master key made central with no claim set counted as flat for votes and not
for calls (review rv_9dd0 of 9dd0d25d7, F4).  Both ask this now.

Whether this node is central is the second rule here (central_standing),
for the same reason: the API gate read the claim alone while /prompts/sync
asked the proof, so one request was judged by two rules (review rv_82eb).

Not in security/key_delegation.py beside get_node_tier: that file is one of
the guardrails' PROTECTED_FILES.
"""
import logging
import os

logger = logging.getLogger(__name__)

# The tiers that count as flat.  'local' joined 2026-11-17, owner (verbatim):
# "Should a node set to "local" count as flat?- yes but can accept peers
# from the same network".  key_delegation.get_node_tier returns 'local' as
# claimed (no certificate is asked for it), so the proof half reads it too.
FLAT_TIERS = frozenset({'flat', 'local'})


def is_flat_node() -> bool:
    """True only when this node is flat both as CLAIMED and as PROVEN.

    The claim (HEVOLVE_NODE_TIER; unset or empty reads 'flat', as
    key_delegation.get_node_tier reads it) withholds flat: the product's own
    promotions set 'regional' with no certificate and leave an upgraded
    central with no key, and get_node_tier, which asks what a node has
    PROVEN, answers 'flat' for both.  The proof catches the converse, a node
    promoted to central by its key with no claim set.  'flat' and 'local'
    (FLAT_TIERS) count; any other claim -- regional, central, a value no
    rule knows -- is not flat.  get_node_tier is asked only when the claim
    counts."""
    claimed = (os.environ.get('HEVOLVE_NODE_TIER') or 'flat').strip().lower()
    if claimed not in FLAT_TIERS:
        return False
    from security.key_delegation import get_node_tier
    return get_node_tier() in FLAT_TIERS


# True once this process has found its central claim unproven
# (central_standing).  The proof is the process's own environment, which
# nothing sets after start, so the answer holds until a restart while the
# claim stays; get_node_tier, which logs CRITICAL each time it refuses the
# claim, is then not asked again.
_central_claim_unproven = False


def central_standing() -> str:
    """Whether this node is central, as CLAIMED and as PROVEN, beside
    is_flat_node.  The one rule the API gate (security.middleware.
    check_api_auth) and /prompts/sync (hart_intelligence_entry) ask; each
    asked its own until 11-22: the gate the bare claim, the route a copy of
    this (parallel-path audit H6, review rv_82eb of 82eb57889).

    'proven'    the master key proves central (key_delegation.get_node_tier,
                which sets the claim when it does).
    'unproven'  HEVOLVE_NODE_TIER=central with no key proof: neither
                central, so it keeps no owner's data, nor flat, so it opens
                nothing.  Logged CRITICAL once, with what to set.
    ''          not central: no claim and no proof.

    get_node_tier is asked only when the claim counts (central, or
    FLAT_TIERS, where the key alone can prove central), as is_flat_node
    asks it: for a regional claim with no certificate it warns on every
    call, and the gate asks this on every request."""
    global _central_claim_unproven
    claimed = (os.environ.get('HEVOLVE_NODE_TIER') or 'flat').strip().lower()
    if claimed == 'central' and _central_claim_unproven:
        return 'unproven'
    if claimed != 'central' and claimed not in FLAT_TIERS:
        return ''
    from security.key_delegation import get_node_tier
    if get_node_tier() == 'central':
        return 'proven'
    if claimed != 'central':
        return ''
    _central_claim_unproven = True
    logger.critical(
        "this node sets HEVOLVE_NODE_TIER=central but holds no valid "
        "HEVOLVE_MASTER_PRIVATE_KEY, so it is neither central nor flat: it "
        "asks every caller for a login and keeps no recipe bundles. Set the "
        "master key, or remove HEVOLVE_NODE_TIER=central, then restart it")
    return 'unproven'
