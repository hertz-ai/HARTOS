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


def one_vote_per_identity(ballots):
    """Collapse vote rows into ONE vote per identity (owner ruling: an agent
    counts as its owner, and no one entity monopolises the hive).

    ``ballots``: iterable of dicts {'identity', 'own', 'value', 'weight'},
    one per vote row; ``own`` is True for the identity's human voting as
    themself.  Returns {identity: (value, weight)}:
      - the human's own vote when they cast one (their agents are advisory);
      - otherwise the majority side of that identity's votes (FOR vs
        AGAINST), its value and weight the mean of that side's;
      - a tie between FOR and AGAINST is no vote: (0, 0.0);
      - only abstains: an abstain, (0, mean weight).
    Measured before this (review of 058c01b05): five people 2 FOR / 3
    AGAINST, one FOR owning ten agents voting FOR, summed per ROW to a
    0.727 FOR share and an approval.  Per identity it is 0.4.
    """
    groups = {}
    for b in ballots:
        groups.setdefault(b['identity'], []).append(b)

    def _mean(bs, key):
        return sum(b[key] for b in bs) / len(bs)

    out = {}
    for identity, bs in groups.items():
        own = [b for b in bs if b['own']]
        if own:
            side = own
        else:
            fors = [b for b in bs if b['value'] > 0]
            againsts = [b for b in bs if b['value'] < 0]
            if len(fors) != len(againsts):
                side = fors if len(fors) > len(againsts) else againsts
            elif fors:
                out[identity] = (0, 0.0)   # split identity: no vote
                continue
            else:
                side = bs                  # only abstains
        out[identity] = (_mean(side, 'value'), _mean(side, 'weight'))
    return out


def quorum_met(distinct_voters: int, distinct_supporters: int) -> bool:
    """True when enough distinct identities voted, and enough voted FOR."""
    return (distinct_voters >= MIN_DISTINCT_VOTERS
            and distinct_supporters >= MIN_DISTINCT_SUPPORTERS)


# PRODUCT_MAP §10: at least 2/3 of the DECISIVE (for + against) weight must
# be FOR.  Abstains are excluded from the denominator.
SUPERMAJORITY_RATIO = 2.0 / 3.0


def is_steward(user) -> bool:
    """True when a vote by ``user`` (a users row, or None) is the steward's.

    The steward is a registered HUMAN account holding the central role,
    the one auth.require_central admits (auth.holds_central_role); no other
    role store.  An agent is never the steward, whoever owns it and
    whatever its own row says: an agent counts as its owner for the quorum
    (tally_votes), never as the steward.  A voter id with no users row --
    the literal 'steward' included -- is no one.  Only a signed-in human
    may cast this vote: the agent tool casts agent votes only
    (thought_experiment_tools.cast_experiment_vote)."""
    if user is None or getattr(user, 'user_type', None) != 'human':
        return False
    from .auth import holds_central_role
    return holds_central_role(user)


def approval_verdict(tally: dict) -> dict:
    """The ONE approval rule for a thought experiment, read from a tally.

    `tally` is ThoughtExperimentService.tally_votes' result.  Approved
    requires all of:
      - quorum_met is True: >= MIN_DISTINCT_VOTERS identities, >=
        MIN_DISTINCT_SUPPORTERS of them FOR, an agent counting as its owner
        (a tally that does not answer it fails closed);
      - total_for / (total_for + total_against) >= the threshold, which is
        max(SUPERMAJORITY_RATIO, the context's approval_threshold): the
        owner's 2/3 is the floor, and a context may only raise it (0.8 for
        security_guardrail);
      - when the context is steward_required, the steward voted FOR
        (tally['steward_vote'] > 0; tally_votes fills it only from votes
        whose voter is_steward, the most negative when several did, so a
        steward's AGAINST is never outvoted by another's FOR).
    The context is the tally's decision_context, and its rules come from
    VOTER_RULES here, never from the tally, so a tally cannot carry a
    weaker threshold.  No decision_context means DEFAULT_RULES, as
    get_voter_rules gives every unknown context.

    Every caller that turns a vote into action asks this: auto-evolve's
    ranking, the evaluation-goal writer, and tally_votes'
    decision_recommendation, so no two of them can disagree.  A caller may
    ADD a stricter floor (auto-evolve's min_approval_score); none may skip
    it.  test_one_approval_rule.py fails if a second rule appears.

    Returns {'approved', 'reason', 'quorum_met', 'super_majority',
    'threshold', 'steward_required', 'steward_approved', 'steward_missing'};
    reason is one of 'approved', 'no_quorum', 'below_threshold',
    'steward_required'.  steward_missing is True when the context requires
    the steward and no steward has answered FOR or AGAINST (an abstain is
    no answer): decide() asks it, so deciding and approving read the
    steward from this one rule.
    """
    rules = get_voter_rules(tally.get('decision_context'))
    threshold = max(SUPERMAJORITY_RATIO, rules['approval_threshold'])
    steward_required = bool(rules['steward_required'])
    steward_vote = tally.get('steward_vote') or 0
    steward_approved = steward_vote > 0
    total_for = tally.get('total_for', 0) or 0
    total_against = tally.get('total_against', 0) or 0
    decisive = total_for + total_against
    ratio = (total_for / decisive) if decisive > 0 else 0.0
    quorate = tally.get('quorum_met') is True
    if not quorate:
        reason = 'no_quorum'
    elif ratio < threshold:
        reason = 'below_threshold'
    elif steward_required and not steward_approved:
        reason = 'steward_required'
    else:
        reason = 'approved'
    return {
        'approved': reason == 'approved',
        'reason': reason,
        'quorum_met': quorate,
        'super_majority': round(ratio, 4),
        'threshold': round(threshold, 4),
        'steward_required': steward_required,
        'steward_approved': steward_approved,
        'steward_missing': steward_required and steward_vote == 0,
    }


def recommendation(tally: dict) -> str:
    """tally_votes' decision_recommendation, read from approval_verdict so
    the two cannot disagree: 'approve' exactly when it approves;
    'no_quorum' and 'steward_required' as it says; otherwise 'reject' when
    the AGAINST share of the decisive weight reaches the threshold, else
    'inconclusive'."""
    verdict = approval_verdict(tally)
    if verdict['approved']:
        return 'approve'
    if verdict['reason'] in ('no_quorum', 'steward_required'):
        return verdict['reason']
    decisive = (tally.get('total_for', 0) or 0) + \
        (tally.get('total_against', 0) or 0)
    against_share = 1.0 - verdict['super_majority'] if decisive > 0 else 0.0
    return 'reject' if against_share >= verdict['threshold'] else 'inconclusive'


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
