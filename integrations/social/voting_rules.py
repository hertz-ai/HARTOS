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
#   - an identity is a registered user; an agent counts as the person at
#     the root of its owner chain, so one person with many agents is still
#     one identity, and an agent no person owns counts as this node, its
#     owner when known (voter_identity, node_identity);
#   - only a decisive vote (non-zero value, weight > 0) counts.
# MIN_DISTINCT_SUPPORTERS = 2 is the invariant itself: one identity can never
# approve alone.  MIN_DISTINCT_VOTERS = 3 is the smallest decisive quorum at
# which the 2/3 super-majority is not simply unanimity, so at least one voter
# beyond the supporters has had a say.
MIN_DISTINCT_VOTERS = 3
MIN_DISTINCT_SUPPORTERS = 2


# Who a vote counts as.  Owner rulings: an agent counts as the human who owns
# it (2026-09-24), and an agent no PERSON owns counts as this node, ONE
# identity per node, "this node's owner" (2026-10-21).  Measured before
# (production, Oct 21): 302 of 305 agents never traced to a person (285
# per-flow agents and hevolve_system_agent with no owner, 16 of the 19 owned
# agents owned by another agent), and counting each as its own identity let
# three of them approve an experiment with no person voting (hevolveai Master
# 11.462 K).  A person is any account whose type is not in
# core.constants.NON_PERSON_USER_TYPES ('agent', 'system'), the same test
# UserService.person_to_notify uses.
#: The identity of every non-person voter whose owner chain reaches no person,
#: when this node's owner is not known as a person here (node_identity).
NODE_IDENTITY = ('node', 'local')
#: The world model's voter (ThoughtExperimentService.ensure_world_model_voter)
#: is one identity of its own (owner ruling 2026-10-19), however many
#: accounts carry its marker.
WORLD_MODEL_IDENTITY = ('world_model', 'local')
#: The settings key that marks the world model's voter account.
WORLD_MODEL_MARKER = 'world_model_voter'


def is_person(user) -> bool:
    """A person: an account whose type is not in core.constants.
    NON_PERSON_USER_TYPES (the test UserService.person_to_notify uses)."""
    from core.constants import NON_PERSON_USER_TYPES
    return getattr(user, 'user_type', None) not in NON_PERSON_USER_TYPES


def vote_is_persons(user, stored_voter_type) -> bool:
    """Whether a vote row is a person's: from its account (is_person) when
    the voter has one, else from the voter_type stored on the row (an
    unregistered voter id: in-process callers).  The ONE reading the tally,
    the vote route's stored label and the discovery metrics share (review of
    40aaed434, finding 1: the route and the metrics read user_type == 'agent'
    and the stored label, so a 'system' account was stored, paid and counted
    as a human while the tally weighed it as an agent)."""
    return is_person(user) if user is not None else stored_voter_type == 'human'


def node_identity(owner_of):
    """This node's identity in the tally: its owner, the person whose
    machine this is (HEVOLVE_OWNER_USER_ID, which Nunba exports at boot and
    integrations/vlm/safety.py reads as "whose machine this is"), when that
    id is a person's row here; otherwise NODE_IDENTITY.  A guest id
    (g_<hex>) or an operator pin with no row here, an agent or system id,
    and an unset variable (a HARTOS node without Nunba) all fall back.  So
    the owner's own vote and their node's agents are ONE identity: the
    owner plus their own machine can never approve alone."""
    import os
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    if owner:
        row = owner_of(owner)
        if row is not None and is_person(row):
            return row.id
    return NODE_IDENTITY


def voter_identity(user, owner_of, node=NODE_IDENTITY):
    """The identity a registered voter's vote counts as, for the quorum and
    for one_vote_per_identity.

      - a person is themself (``user.id``);
      - the world model's voter (an agent with no owner, settings
        WORLD_MODEL_MARKER) is WORLD_MODEL_IDENTITY;
      - any other account that is not a person (an agent, a system account)
        is the PERSON at the root of its owner chain (an agent may be owned
        by an agent, which may be owned by a person);
      - one whose chain reaches no person -- no owner, an agent-only chain,
        a system account at its root, a cycle, a missing row -- is ``node``
        (node_identity: this node's owner, else NODE_IDENTITY).

    Deliberate differences from UserService.person_to_notify (same person
    test): the vote walks the whole chain (a notification stops at one hop),
    and an owner id with no row here is this node, not that id (a
    notification may be for a person on another node; a vote may not name
    an identity this node cannot see, or any made-up owner id would be a
    free identity).  ``owner_of(owner_id)`` returns that users row, or None.
    """
    if is_person(user):
        return user.id
    if (getattr(user, 'user_type', None) == 'agent' and user.owner_id is None
            and (getattr(user, 'settings', None) or {}).get(WORLD_MODEL_MARKER) is True):
        return WORLD_MODEL_IDENTITY
    seen = {user.id}
    owner_id = user.owner_id
    while owner_id is not None and owner_id not in seen:
        seen.add(owner_id)
        owner = owner_of(owner_id)
        if owner is None:
            break
        if is_person(owner):
            return owner.id
        owner_id = owner.owner_id
    return node


def one_vote_per_identity(ballots):
    """Collapse vote rows into ONE vote per identity (owner rulings: an agent
    counts as the person at the root of its owner chain, or as this node;
    voter_identity; and no one entity monopolises the hive).

    ``ballots``: iterable of dicts {'identity', 'own', 'value', 'weight'},
    one per vote row; ``own`` is True for the identity's PERSON voting as
    themself (never for an agent or a system account: is_person).
    Returns {identity: (value, weight)}:
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


def identities_holding(ballots) -> int:
    """How many identities hold back an experiment that would otherwise
    evaluate with no vote (evaluation_permit's own path).  An identity holds
    when ANY of its ballots is AGAINST -- any weight, 0 included, and an
    unregistered voter id too -- unless that identity's PERSON voted FOR as
    themself (``own``).  ``ballots`` as one_vote_per_identity takes.

    Counted from the ballots, not from one_vote_per_identity, where a tie
    between a FOR and an AGAINST of one identity is no vote and a majority
    of FORs wins: /auth/register makes an agent for anyone with any owner_id
    (a made-up one maps to this node), so a stranger's agent FOR cancelled,
    or outvoted, the owner's agent's AGAINST (claude-autostart-d2).  So only
    that identity's person, or the voter who cast the AGAINST, releases it.
    It only ever holds something back, so it counts on the safe side, unlike
    the quorum, where a made-up id must not be a free voter."""
    own_for = {b['identity'] for b in ballots if b['own'] and b['value'] > 0}
    return len({b['identity'] for b in ballots
                if b['value'] < 0 and b['identity'] not in own_for})


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
    whatever its own row says: an agent counts as the person at the root
    of its owner chain, or as this node, for the quorum (voter_identity),
    never as the steward.  A voter id with no users row --
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
        MIN_DISTINCT_SUPPORTERS of them FOR, each counted by voter_identity
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


# ─── Evaluating here without a vote: flat nodes, this node's own ─────
#
# Owner rulings (verbatim): 2026-11-14 "voting shd not be a prerequisite to
# evolve locally untill we cross the intelligence barrier"; 2026-11-15 "the
# no ovite experiment applies only for topology flat, for regional and
# central it shd remain with voites".  The intelligence barrier has no
# measure yet (none in code, docs or memory on 2026-11-14); when the owner
# defines one, evaluation_permit is the one place the vote returns.


def evolves_without_vote_here() -> bool:
    """Whether this node's own experiments may evaluate here with no vote:
    only on a flat node (owner ruling 2026-11-15), flat by the one rule the
    tree has for "one person's machine" (node_topology.is_flat_node: the
    claim HEVOLVE_NODE_TIER says flat, unset or empty, AND the proof
    key_delegation.get_node_tier says flat).  A claim of regional or central
    keeps the vote whatever was proven; a node its master key made central
    keeps it whatever was claimed (review rv_9dd0 F4: the claim alone missed
    it).  'local' counts as flat (owner 2026-11-17, node_topology.
    FLAT_TIERS); any other claim -- a value no rule knows -- keeps it."""
    from .node_topology import is_flat_node
    return is_flat_node()


def node_holds(user, owner_of) -> bool:
    """Whether ``user`` (a users row, or None) is an account only this node
    holds, so an experiment it created is this node's OWN:
      - this node's owner, the person HEVOLVE_OWNER_USER_ID names
        (node_identity), or
      - the node's own machine account as UserService.ensure_system_user
        makes it (the 'nunba' account): user_type 'system', owned by no
        one, no password, is_verified.  Its api_token is a credential, but
        no route hands it out: the only replies that carry a token
        (/auth/register, agent create, /bots/register) return the account
        that request just made.
    Never an agent: POST /auth/register makes an agent with no sign-in for
    anyone and takes its owner_id from the request body, so "owned by the
    owner" is a claim anyone can make (review of e7b769936, which counted
    every account no person owns as the node).  Never a 'system' account
    with a password: the same route takes user_type from the body and
    always sets one, so that is someone's sign-in.  Never a row an inbound
    sync made: sync_engine._handle_sync_user creates users with the
    payload's own id and user_type, no owner, no password, and never sets
    is_verified ("verification is a trusted local act").
    ``owner_of(owner_id)`` returns that users row, or None."""
    if user is None:
        return False
    if is_person(user):
        return node_identity(owner_of) == user.id
    return (getattr(user, 'user_type', None) == 'system'
            and getattr(user, 'owner_id', None) is None
            and not getattr(user, 'password_hash', None)
            and getattr(user, 'is_verified', None) is True)


def _permit_hint(reason: str, verdict: dict, tally: dict) -> str:
    """What a permit's reason means and what would let the experiment run,
    in words, for whoever asked (owner ruling 2026-11-09: a refusal says
    what was wrong and how to fix it).  The vote's numbers are the rule's
    own (the constants above, the verdict's threshold and steward), never a
    copy that drifts from them (review rv_9dd0 F6)."""
    vote = (f'the vote approves it: >= {MIN_DISTINCT_VOTERS} distinct voters, '
            f'>= {MIN_DISTINCT_SUPPORTERS} FOR, a FOR share of at least '
            f"{verdict['threshold']:.2f}"
            + (", and the steward's FOR" if verdict['steward_required'] else ''))
    opponents = tally.get('identities_against')
    who = (f'{opponents} voter(s)' if isinstance(opponents, int)
           else 'the tally does not say how many voters; any')
    return {
        'approved': 'the vote approved it',
        'this_nodes_own': "this node's own experiment, asked for by this "
                          'node, evaluated here with no vote (flat node)',
        'not_this_nodes_own': f"not this node's own experiment: it runs once "
                              f'{vote}',
        'topology_keeps_the_vote': 'this node is not flat or local '
                                   '(HEVOLVE_NODE_TIER names another tier, or '
                                   'its key proves one): on regional and '
                                   'central every '
                                   f'experiment runs only once {vote}',
        'not_asked_by_this_node': "this node's own experiment runs with no vote "
                                  'only when this node starts it: its '
                                  'auto-evolve cycle, or its owner signed in. '
                                  f'Asked by anyone else, it runs once {vote}',
        'steward_required': 'a security decision: even this node\'s own runs '
                            f'only once {vote}',
        'vote_against': f'{who} voted against it: it runs once {vote}, or '
                        'with no vote again once every vote against is '
                        'changed to FOR or to an abstain (0), or the person '
                        "an agent's vote counts as votes FOR themself",
    }[reason]


def evaluation_permit(tally: dict, *, this_nodes_own: bool,
                      asked_by_this_node: bool) -> dict:
    """Whether an experiment may be EVALUATED on this node: the ONE rule the
    evaluation-goal writer and auto-evolve's ranking both ask.

      - the vote approved it (approval_verdict): yes, whoever asks, on every
        topology;
      - otherwise yes only when ALL hold:
          * the experiment is this node's own (``this_nodes_own``: its
            creator is an account node_holds);
          * this is a flat node (evolves_without_vote_here);
          * THIS NODE asked (``asked_by_this_node``: its own auto-evolve
            cycle, or an account node_holds -- the owner signed in).  On
            9dd0d25d7 the permit never asked who asked, and any signed-in
            account, a passwordless agent from /auth/register included,
            started the node's own goals ten at once, past the cycle's pace
            (review rv_9dd0, F1);
          * its context does not require the steward (security stays with
            people);
          * no voter is against it: tally['identities_against'] == 0
            (identities_holding: any AGAINST ballot of an identity, at any
            weight and registered or not, unless that identity's person
            voted FOR as themself).  total_against is rounded to 2 places,
            so an agent's AGAINST weighing under 0.005 read as none and did
            not hold it (rv_9dd0, F2); and it is counted after
            one_vote_per_identity, where another agent's FOR could cancel
            an AGAINST.  A tally that does not answer holds it.

    What this decides is whether the evaluation goal is made; decide() keeps
    the vote.  Returns {'allowed', 'approved_by_vote', 'own_without_vote',
    'reason', 'hint', 'verdict'}: reason is 'approved', 'this_nodes_own', or
    the first own-path condition that failed ('not_this_nodes_own',
    'topology_keeps_the_vote', 'not_asked_by_this_node', 'steward_required',
    'vote_against'); hint says it in words and what would let it run;
    verdict is approval_verdict(tally)."""
    verdict = approval_verdict(tally)
    if not this_nodes_own:
        why = 'not_this_nodes_own'
    elif not evolves_without_vote_here():
        why = 'topology_keeps_the_vote'
    elif not asked_by_this_node:
        why = 'not_asked_by_this_node'
    elif verdict['steward_required']:
        why = 'steward_required'
    elif tally.get('identities_against') != 0:
        why = 'vote_against'
    else:
        why = 'this_nodes_own'
    own_without_vote = why == 'this_nodes_own'
    reason = 'approved' if verdict['approved'] else why
    return {
        'allowed': verdict['approved'] or own_without_vote,
        'approved_by_vote': verdict['approved'],
        'own_without_vote': own_without_vote,
        'reason': reason,
        'hint': _permit_hint(reason, verdict, tally),
        'verdict': verdict,
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
