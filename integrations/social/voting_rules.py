"""
Context-Based Constitutional Voting Rules.

Decision contexts determine who can vote and how votes are weighted.
Security changes require human-only votes; operational tuning can be
agent-decided with human override.

Integrates with ThoughtExperimentService.cast_vote() and tally_votes().
"""

# ─── Voter Rule Definitions ──────────────────────────────────────────

VOTER_RULES = {
    'security_guardrail': {
        'agent_can_vote': False,
        'human_required': True,
        'agent_weight': 0.0,
        'human_weight': 1.0,
        'approval_threshold': 0.8,
        'steward_required': True,
    },
    'technical_improvement': {
        'agent_can_vote': True,
        'human_required': True,
        'agent_weight': 0.6,
        'human_weight': 1.0,
        'approval_threshold': 0.5,
        'steward_required': False,
    },
    'business_revenue': {
        'agent_can_vote': True,
        'human_required': True,
        'agent_weight': 0.8,
        'human_weight': 1.0,
        'approval_threshold': 0.5,
        'steward_required': False,
    },
    'operational_tuning': {
        'agent_can_vote': True,
        'human_required': False,
        'agent_weight': 1.0,
        'human_weight': 1.0,
        'approval_threshold': 0.3,
        'steward_required': False,
    },
}

# Default rules for unclassified decisions
DEFAULT_RULES = {
    'agent_can_vote': True,
    'human_required': True,
    'agent_weight': 0.6,
    'human_weight': 1.0,
    'approval_threshold': 0.5,
    'steward_required': False,
}

# ─── Quorum: no single identity approves ─────────────────────────────
#
# Owner principle (2026-09-24): nobody should ever have to worry about one
# person monopolising AI.  A ratio gate alone cannot express that -- 1 FOR /
# 0 AGAINST is a ratio of 1.0, and weighting lets one human FOR outweigh two
# low-confidence agents AGAINST (1.0 / 1.4 = 0.71 >= 2/3).  So approval also
# needs DISTINCT identities, counted by tally_votes:
#   - an identity is a registered user; an agent counts as the human who
#     owns it, so one person with many agents is still one identity;
#   - only a decisive vote (non-zero value, weight > 0) counts.
# MIN_DISTINCT_SUPPORTERS = 2 is the invariant itself: one identity can never
# approve alone.  MIN_DISTINCT_VOTERS = 3 is the smallest decisive quorum at
# which the 2/3 super-majority is not simply unanimity, so at least one voter
# beyond the supporters has had a say.
MIN_DISTINCT_VOTERS = 3
MIN_DISTINCT_SUPPORTERS = 2


def quorum_met(distinct_voters: int, distinct_supporters: int) -> bool:
    """True when enough distinct identities voted, and enough voted FOR."""
    return (distinct_voters >= MIN_DISTINCT_VOTERS
            and distinct_supporters >= MIN_DISTINCT_SUPPORTERS)


# ─── Context Classification ──────────────────────────────────────────

# Keywords that map to decision contexts (checked against title + hypothesis)
_CONTEXT_KEYWORDS = {
    'security_guardrail': [
        'security', 'guardrail', 'master key', 'circuit breaker',
        'kill switch', 'permission', 'access control', 'authentication',
        'certificate', 'encryption', 'firewall', 'vulnerability',
    ],
    'technical_improvement': [
        'performance', 'optimization', 'refactor', 'architecture',
        'algorithm', 'latency', 'throughput', 'scalability',
        'bug fix', 'improvement', 'upgrade', 'migration',
    ],
    'business_revenue': [
        'revenue', 'pricing', 'monetization', 'subscription',
        'trading', 'investment', 'profit', 'ad revenue',
        'business model', 'marketplace', 'commercial',
    ],
    'operational_tuning': [
        'threshold', 'timeout', 'interval', 'batch size',
        'cache', 'tuning', 'parameter', 'configuration',
        'polling', 'retry', 'rate limit',
    ],
}


def classify_decision_context(experiment_dict: dict) -> str:
    """Classify a thought experiment into a decision context.

    Scans title + hypothesis for keywords. Returns the best-matching
    context or 'technical_improvement' as default.
    """
    text = ' '.join([
        (experiment_dict.get('title') or ''),
        (experiment_dict.get('hypothesis') or ''),
    ]).lower()

    scores = {}
    for context, keywords in _CONTEXT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[context] = score

    if not scores:
        return 'technical_improvement'

    return max(scores, key=scores.get)


def get_voter_rules(context: str) -> dict:
    """Return voter rules for a decision context."""
    return VOTER_RULES.get(context, DEFAULT_RULES)


def check_voter_eligibility(experiment_dict: dict, voter_type: str) -> dict:
    """Check if a voter is eligible to vote on an experiment.

    Returns: {'eligible': bool, 'reason': str, 'context': str, 'rules': dict}
    """
    context = experiment_dict.get('decision_context') or \
        classify_decision_context(experiment_dict)
    rules = get_voter_rules(context)

    # Humans can always vote
    if voter_type == 'human':
        return {
            'eligible': True,
            'reason': 'human_always_eligible',
            'context': context,
            'rules': rules,
        }

    # Agent eligibility depends on context
    if not rules['agent_can_vote']:
        return {
            'eligible': False,
            'reason': f'agents_cannot_vote_on_{context}',
            'context': context,
            'rules': rules,
        }

    return {
        'eligible': True,
        'reason': 'agent_eligible',
        'context': context,
        'rules': rules,
    }
