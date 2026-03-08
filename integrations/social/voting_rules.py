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
