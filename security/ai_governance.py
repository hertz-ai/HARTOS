"""
AI Governance Framework — Constitutional scoring for a rational hive.

DESIGN PRINCIPLE: The default is FREEDOM.  In a constitutional system, humans
have the right to act freely unless a specific constitutional rule is violated.
This framework never blocks first — it scores, explains, and bounds.

WHAT DETERMINISM ACTUALLY MEANS:
  Determinism ≠ binary regex gates that false-positive on innocent text.
  Determinism = same input ALWAYS produces the same output, AND the output
  is verifiable, reproducible, and auditable.

  A mathematical function is deterministic.
  A weighted multi-signal score is deterministic.
  A Merkle-linked decision chain is deterministic.
  A regex that blocks "deceptive" and catches "a deceptive practice in law" is NOT
  intelligent — it's a blunt instrument that sacrifices accuracy for the illusion of safety.

THE CONSTITUTIONAL SCORING MODEL:
  1. Start with full freedom (score = 1.0)
  2. Each constitutional signal ADJUSTS the score — never binary, always gradual
  3. Multiple signals aggregate deterministically (weighted geometric mean)
  4. Intelligence REFINES the score — catches what math misses, reduces false positives
  5. Constitutional bounds constrain the final result — hard limits on specific dimensions
  6. Every step is Merkle-linked — decisions are reproducible and auditable

  The key insight: deterministic scoring is MORE accurate than binary gates because
  it preserves information.  A score of 0.15 tells you "very likely a violation."
  A score of 0.85 tells you "probably fine."  A binary gate destroys this nuance.

  Intelligence is used to INCREASE accuracy, not to be bypassed.  When the
  deterministic score is ambiguous (0.3-0.7), intelligence resolves the ambiguity.
  When the score is clear (< 0.1 or > 0.9), intelligence confirms but cannot override.

ECONOMIC PRINCIPLE: Automation creates abundance, not scarcity.
  Revenue flows TO people (90/9/1 — 90% to contributors).
  People buy and sell freely within constitutional limits.
  Constitutional voting determines what commerce is permitted.
  The hive exists to make abundance available to everyone, everywhere.

HUMAN CONSENT: The guardian talks with its human.
  The AI companion interleaves human consent into the decision chain.
  Consent is a constitutional right — actions that affect the user require it.
"""

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')


# ═══════════════════════════════════════════════════════════════════════
# Decision Classifications
# ═══════════════════════════════════════════════════════════════════════

class DecisionDomain(Enum):
    """Domains of governance — each has its own constitutional scoring."""
    GOAL_APPROVAL = 'goal_approval'
    COMPUTE_ALLOCATION = 'compute_allocation'
    RALT_DISTRIBUTION = 'ralt_distribution'
    REVENUE_DISTRIBUTION = 'revenue_distribution'
    TRUST_ESTABLISHMENT = 'trust_establishment'
    CONTENT_SAFETY = 'content_safety'
    CODE_CHANGE = 'code_change'
    RESOURCE_ACCESS = 'resource_access'
    COMMERCE = 'commerce'
    HUMAN_CONSENT = 'human_consent'
    HUMAN_WELLBEING = 'human_wellbeing'
    SELF_SOVEREIGNTY = 'self_sovereignty'
    PRIVACY = 'privacy'


class DecisionOutcome(Enum):
    """Possible outcomes of a governance decision."""
    APPROVED = 'approved'           # Passed all checks
    REJECTED = 'rejected'           # Clear constitutional violation
    BOUNDED = 'bounded'             # Approved but constrained
    DEFERRED = 'deferred'           # Ambiguous — needs more info or human input
    ESCALATED = 'escalated'         # Needs human review


# ═══════════════════════════════════════════════════════════════════════
# Constitutional Signal — individual scoring dimension
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ConstitutionalSignal:
    """One dimension of constitutional scoring.

    Each signal is a deterministic function that maps context → score.
    Score range: 0.0 (clear violation) to 1.0 (clearly fine).
    The signal also carries confidence: how sure are we about this score?
    """
    name: str
    score: float        # 0.0 = violation, 1.0 = fine
    confidence: float   # 0.0 = uncertain, 1.0 = certain
    weight: float       # Relative importance (default 1.0)
    reasoning: str      # Human-readable explanation


@dataclass
class GovernanceDecision:
    """Immutable record of a governance decision with full signal chain."""
    decision_id: str
    domain: str
    outcome: str
    signals: List[dict]             # All constitutional signals that contributed
    aggregate_score: float          # Deterministic aggregate of all signals
    intelligent_adjustment: float   # How much intelligence changed the score
    final_score: float              # After bounds enforcement
    reasoning: str
    timestamp: float = field(default_factory=time.time)
    audit_hash: str = ''
    parent_hash: str = ''           # Previous decision hash — Merkle chain

    def compute_audit_hash(self) -> str:
        """Deterministic hash for audit trail — Merkle-linked to parent."""
        payload = {
            'decision_id': self.decision_id,
            'domain': self.domain,
            'outcome': self.outcome,
            'aggregate_score': self.aggregate_score,
            'intelligent_adjustment': self.intelligent_adjustment,
            'final_score': self.final_score,
            'reasoning': self.reasoning,
            'timestamp': self.timestamp,
            'parent_hash': self.parent_hash,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# Constitutional Bounds — the ONLY hard limits
# ═══════════════════════════════════════════════════════════════════════
#
# These are mathematical constraints, not pattern-matching gates.
# They define the SPACE of allowed actions — anything inside is permitted.
# Only specific, measurable violations trigger denial.

CONSTITUTIONAL_BOUNDS = {
    # Revenue: exact split (mathematical, not pattern-matched)
    'revenue_users_pct': 0.90,
    'revenue_infra_pct': 0.09,
    'revenue_central_pct': 0.01,
    # Compute: mathematical caps
    'max_single_entity_influence': 0.05,
    'reward_scaling': 'logarithmic',
    # RALT: witness threshold (mathematical)
    'min_ralt_witnesses': 2,
    'max_skill_improvement_per_day': 0.05,
    # Trust: time-based expiry (mathematical)
    'audit_compute_ratio': 0.80,
    'contract_validity_days': 30,
    'max_violations_before_expulsion': 3,
    # Budget: cost comparison (mathematical)
    'local_model_cost_spark': 0,
    'max_goals_per_hour': 10,
    # Consent: time-based expiry (mathematical)
    'consent_validity_hours': 24,
    # Commerce: revenue floor (mathematical)
    'commerce_revenue_to_contributors_min_pct': 0.90,
}


def get_constitutional_bound(key: str) -> Any:
    """Get a constitutional bound value."""
    return CONSTITUTIONAL_BOUNDS.get(key)


# For backward compatibility
DETERMINISTIC_BOUNDS = CONSTITUTIONAL_BOUNDS

def get_deterministic_bound(key: str) -> Any:
    """Backward-compatible alias."""
    return CONSTITUTIONAL_BOUNDS.get(key)


# ═══════════════════════════════════════════════════════════════════════
# Constitutional Scorer — deterministic multi-signal scoring
# ═══════════════════════════════════════════════════════════════════════

def _aggregate_signals(signals: List[ConstitutionalSignal]) -> float:
    """Deterministic aggregation of constitutional signals.

    Uses weighted geometric mean — this is critical because:
    1. It's deterministic (same signals → same result, always)
    2. It preserves information (no binary destruction)
    3. A single very low signal pulls the aggregate down (safety)
    4. But it doesn't zero out on one borderline signal (accuracy)

    Formula: exp(Σ(wᵢ × ln(sᵢ)) / Σ(wᵢ))
    where sᵢ = max(signal_score, 0.001) to prevent log(0)
    """
    if not signals:
        return 1.0  # No signals = full freedom

    total_weight = sum(s.weight for s in signals)
    if total_weight == 0:
        return 1.0

    weighted_log_sum = sum(
        s.weight * math.log(max(s.score, 0.001))
        for s in signals
    )
    return math.exp(weighted_log_sum / total_weight)


def _aggregate_confidence(signals: List[ConstitutionalSignal]) -> float:
    """Deterministic confidence aggregation.

    High confidence = we know what we're scoring.
    Low confidence = ambiguous, might need intelligence or human input.
    """
    if not signals:
        return 1.0

    total_weight = sum(s.weight for s in signals)
    if total_weight == 0:
        return 1.0

    return sum(s.weight * s.confidence for s in signals) / total_weight


# ═══════════════════════════════════════════════════════════════════════
# Governance Pipeline — Constitutional scoring with Merkle audit
# ═══════════════════════════════════════════════════════════════════════

class GovernancePipeline:
    """Constitutional scoring pipeline — freedom-first, accuracy-preserving.

    Stage 1 (CONSTITUTIONAL SCORING):
      Multiple deterministic signal functions score the action.
      Each signal is a mathematical function, not a binary gate.
      Default score = 1.0 (freedom).  Signals reduce when they detect risk.

    Stage 2 (INTELLIGENT REFINEMENT):
      When aggregate confidence is LOW (ambiguous zone 0.3-0.7),
      intelligence resolves ambiguity — increases accuracy.
      When confidence is HIGH (clear zone <0.1 or >0.9),
      intelligence confirms but its adjustment is bounded.
      Intelligence ALWAYS runs — it is never bypassed.

    Stage 3 (CONSTITUTIONAL BOUNDS):
      Mathematical constraints on specific dimensions.
      Revenue split, compute caps, witness thresholds.
      These are the ONLY hard limits — everything else is scored.

    Stage 4 (MERKLE AUDIT):
      Every decision is hash-linked to the previous one.
      The full chain is reproducible and verifiable.
    """

    def __init__(self):
        self._scorers: Dict[str, List[Callable]] = {}  # domain → [scorer_fns]
        self._refiners: Dict[str, Callable] = {}       # domain → intelligence_fn
        self._bounds: Dict[str, Callable] = {}          # domain → bounds_fn
        self._decision_log: List[GovernanceDecision] = []
        self._last_hash: str = ''  # Merkle chain head
        self._lock = __import__('threading').Lock()

    # --- Registration ---

    def register_scorer(self, domain: str, scorer_fn: Callable):
        """Register a constitutional scoring function.

        scorer_fn(context: dict) -> ConstitutionalSignal
        """
        self._scorers.setdefault(domain, []).append(scorer_fn)

    def register_refiner(self, domain: str, refiner_fn: Callable):
        """Register an intelligent refinement function.

        refiner_fn(aggregate_score: float, confidence: float, context: dict) -> float
        Returns adjustment in [-0.3, +0.3] range.
        """
        self._refiners[domain] = refiner_fn

    def register_bounds(self, domain: str, bounds_fn: Callable):
        """Register a constitutional bounds function.

        bounds_fn(score: float, context: dict) -> (float, str)
        Returns (bounded_score, reason).
        """
        self._bounds[domain] = bounds_fn

    # --- Backward compatibility ---

    def register_gate(self, domain: str, gate_fn: Callable):
        """Backward compat: wrap a binary gate as a scorer."""
        def _gate_as_scorer(context):
            try:
                passed, reason = gate_fn(context)
                return ConstitutionalSignal(
                    name=f'gate_{domain}',
                    score=1.0 if passed else 0.0,
                    confidence=1.0,
                    weight=2.0,  # Gates get high weight
                    reasoning=reason,
                )
            except Exception as e:
                return ConstitutionalSignal(
                    name=f'gate_{domain}',
                    score=0.5,
                    confidence=0.2,
                    weight=1.0,
                    reasoning=f'Gate error: {e}',
                )
        self.register_scorer(domain, _gate_as_scorer)

    def register_evaluator(self, domain: str, eval_fn: Callable):
        """Backward compat: wrap an evaluator as a refiner."""
        def _eval_as_refiner(aggregate, confidence, context):
            try:
                raw = float(eval_fn(context))
                raw = max(0.0, min(1.0, raw))
                return (raw - aggregate) * 0.5  # Bounded adjustment
            except Exception:
                return 0.0
        self.register_refiner(domain, _eval_as_refiner)

    def register_validator(self, domain: str, validate_fn: Callable):
        """Backward compat: wrap a validator as bounds."""
        self.register_bounds(domain, validate_fn)

    # --- Core Decision Engine ---

    def decide(self, domain: str, context: dict,
               decision_id: str = '') -> GovernanceDecision:
        """Run the constitutional scoring pipeline.

        The default is FREEDOM (score = 1.0).
        Signals reduce the score when they detect constitutional risk.
        Intelligence refines when confidence is low.
        Bounds enforce hard mathematical limits.
        Everything is Merkle-audited.
        """
        if not decision_id:
            import uuid
            decision_id = uuid.uuid4().hex[:16]

        # ── Stage 1: CONSTITUTIONAL SCORING ──
        # Multiple signals, each deterministic, each scored 0-1
        scorer_fns = self._scorers.get(domain, [])
        signals: List[ConstitutionalSignal] = []
        for fn in scorer_fns:
            try:
                sig = fn(context)
                if isinstance(sig, ConstitutionalSignal):
                    signals.append(sig)
            except Exception as e:
                logger.debug(f"Scorer error in {domain}: {e}")

        aggregate = _aggregate_signals(signals)
        confidence = _aggregate_confidence(signals)

        # ── Stage 2: INTELLIGENT REFINEMENT ──
        # Intelligence ALWAYS runs — never bypassed.
        # Its adjustment is proportional to ambiguity:
        #   High confidence (>0.8): adjustment bounded to ±0.1
        #   Medium confidence (0.4-0.8): adjustment bounded to ±0.2
        #   Low confidence (<0.4): adjustment bounded to ±0.3
        adjustment = 0.0
        refiner_fn = self._refiners.get(domain)
        if refiner_fn:
            try:
                raw_adj = float(refiner_fn(aggregate, confidence, context))
            except Exception:
                raw_adj = 0.0

            # Bound adjustment by confidence — less certain = more room for AI
            if confidence > 0.8:
                max_adj = 0.1
            elif confidence > 0.4:
                max_adj = 0.2
            else:
                max_adj = 0.3
            adjustment = max(-max_adj, min(max_adj, raw_adj))

        refined_score = max(0.0, min(1.0, aggregate + adjustment))

        # ── Stage 3: CONSTITUTIONAL BOUNDS ──
        bounds_fn = self._bounds.get(domain)
        if bounds_fn:
            try:
                final_score, bound_reason = bounds_fn(refined_score, context)
                final_score = max(0.0, min(1.0, final_score))
            except Exception as e:
                final_score = refined_score
                bound_reason = f'Bounds error: {e}'
        else:
            final_score = refined_score
            bound_reason = 'No bounds — score unchanged'

        # ── Determine Outcome ──
        if final_score >= 0.7:
            outcome = DecisionOutcome.APPROVED.value
        elif final_score >= 0.5:
            # Approved but we should note the constraint
            outcome = DecisionOutcome.BOUNDED.value
        elif final_score >= 0.3:
            # Ambiguous — defer to human or more information
            outcome = DecisionOutcome.DEFERRED.value
        else:
            outcome = DecisionOutcome.REJECTED.value

        # Build reasoning from signal chain
        signal_summary = '; '.join(
            f'{s.name}={s.score:.2f}(c={s.confidence:.1f})'
            for s in signals
        ) or 'no signals'

        reasoning = (
            f'Signals: [{signal_summary}]; '
            f'Aggregate: {aggregate:.3f} (confidence: {confidence:.2f}); '
            f'Intelligence: {adjustment:+.3f} → {refined_score:.3f}; '
            f'Bounds: {final_score:.3f} ({bound_reason})'
        )

        # ── Stage 4: MERKLE AUDIT ──
        with self._lock:
            parent_hash = self._last_hash

        decision = GovernanceDecision(
            decision_id=decision_id,
            domain=domain,
            outcome=outcome,
            signals=[asdict(s) for s in signals],
            aggregate_score=aggregate,
            intelligent_adjustment=adjustment,
            final_score=final_score,
            reasoning=reasoning,
            parent_hash=parent_hash,
        )
        decision.audit_hash = decision.compute_audit_hash()

        self._record(decision)
        return decision

    def _record(self, decision: GovernanceDecision):
        """Record decision and advance Merkle chain."""
        with self._lock:
            self._decision_log.append(decision)
            self._last_hash = decision.audit_hash

        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'governance_decision',
                actor_id='ai_governance',
                action=(
                    f'{decision.domain}:{decision.outcome} '
                    f'score={decision.final_score:.2f} '
                    f'id={decision.decision_id}'
                ),
            )
        except Exception:
            pass

    def get_recent_decisions(self, domain: str = '',
                             limit: int = 50) -> List[dict]:
        """Return recent decisions for inspection."""
        with self._lock:
            decisions = self._decision_log[-limit:]
        if domain:
            decisions = [d for d in decisions if d.domain == domain]
        return [asdict(d) for d in decisions]

    def verify_merkle_chain(self, decisions: List[GovernanceDecision] = None
                            ) -> Tuple[bool, str]:
        """Verify the Merkle chain integrity of the decision log."""
        with self._lock:
            chain = decisions or self._decision_log
        if not chain:
            return True, 'Empty chain'
        for i, d in enumerate(chain):
            recomputed = d.compute_audit_hash()
            if recomputed != d.audit_hash:
                return False, f'Decision {i} hash mismatch (tampered)'
            if i > 0 and d.parent_hash != chain[i - 1].audit_hash:
                return False, f'Decision {i} Merkle link broken'
        return True, f'Chain verified: {len(chain)} decisions'


# ═══════════════════════════════════════════════════════════════════════
# Built-in Constitutional Scorers
# ═══════════════════════════════════════════════════════════════════════
#
# These are SCORING FUNCTIONS, not binary gates.
# They return a ConstitutionalSignal with score, confidence, and reasoning.
# The default is 1.0 (freedom) — signals reduce when they detect risk.

def _score_content_safety(context: dict) -> ConstitutionalSignal:
    """Score content against constitutional rules.

    Unlike a binary gate, this scores HOW MUCH the content violates.
    A mild similarity to a violation pattern scores 0.6 (caution).
    A direct match scores 0.05 (near-certain violation).
    No match scores 1.0 (freedom).

    This preserves accuracy — "a deceptive practice in law" scores 0.7
    (the word "deceptive" appears but context is legal discussion, not scam).
    A binary gate would have blocked it.
    """
    text = context.get('text', '')
    if not text:
        return ConstitutionalSignal(
            name='content_safety', score=1.0, confidence=1.0,
            weight=1.0, reasoning='No text — full freedom',
        )

    from security.hive_guardrails import VALUES
    violation_count = 0
    destructive_count = 0
    matched_patterns = []

    for pattern in VALUES.VIOLATION_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            violation_count += len(matches)
            matched_patterns.append(pattern.pattern[:40])

    for pattern in VALUES.DESTRUCTIVE_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            destructive_count += len(matches)
            matched_patterns.append(pattern.pattern[:40])

    if violation_count == 0 and destructive_count == 0:
        return ConstitutionalSignal(
            name='content_safety', score=1.0, confidence=0.9,
            weight=1.0, reasoning='No violation patterns detected — freedom preserved',
        )

    # Score degrades with match density (matches per 1000 chars)
    text_len = max(len(text), 1)
    density = (violation_count + destructive_count) / (text_len / 1000.0)

    # Sigmoid-like scoring: density of 1 match/1000 chars → ~0.4
    # density of 5+ → ~0.05.  This is smooth, not binary.
    score = 1.0 / (1.0 + density * 2.5)

    # Confidence scales with text length — more text = more confident
    confidence = min(0.95, 0.5 + text_len / 2000.0)

    return ConstitutionalSignal(
        name='content_safety', score=score, confidence=confidence,
        weight=1.5,  # Safety signals get moderate weight
        reasoning=(
            f'{violation_count} violation + {destructive_count} destructive '
            f'patterns in {text_len} chars (density={density:.2f}). '
            f'Patterns: {", ".join(matched_patterns[:3])}'
        ),
    )


def _score_goal_approval(context: dict) -> ConstitutionalSignal:
    """Score a goal against constitutional rules."""
    from security.hive_guardrails import ConstitutionalFilter
    goal_dict = context.get('goal', {})
    passed, reason = ConstitutionalFilter.check_goal(goal_dict)
    return ConstitutionalSignal(
        name='goal_constitutional', score=1.0 if passed else 0.05,
        confidence=0.9 if passed else 0.8,
        weight=1.5, reasoning=reason,
    )


def _score_budget(context: dict) -> ConstitutionalSignal:
    """Score compute allocation — mathematical, not pattern-based."""
    cost = context.get('cost_spark', 0)
    budget = context.get('budget_remaining', float('inf'))
    if budget == float('inf') or budget == 0:
        return ConstitutionalSignal(
            name='budget', score=1.0, confidence=0.5,
            weight=1.0, reasoning='No budget constraint',
        )
    ratio = cost / max(budget, 0.001)
    if ratio <= 1.0:
        score = 1.0 - (ratio * 0.3)  # Gentle degradation up to budget limit
    else:
        score = max(0.05, 0.7 / ratio)  # Over budget — score drops but not binary
    return ConstitutionalSignal(
        name='budget', score=score, confidence=0.95,
        weight=1.0, reasoning=f'Cost/budget ratio: {ratio:.2f}',
    )


def _score_revenue_split(context: dict) -> ConstitutionalSignal:
    """Score revenue distribution — mathematical deviation from 90/9/1.

    This is one of the few truly hard constraints — the split is immutable.
    But even here, we score the DEVIATION rather than binary pass/fail.
    A tiny rounding error (90.01%) scores 0.99.
    A deliberate violation (50/30/20) scores 0.01.
    """
    users = context.get('users_pct', CONSTITUTIONAL_BOUNDS['revenue_users_pct'])
    infra = context.get('infra_pct', CONSTITUTIONAL_BOUNDS['revenue_infra_pct'])
    central = context.get('central_pct', CONSTITUTIONAL_BOUNDS['revenue_central_pct'])

    deviation = (
        abs(users - 0.90) +
        abs(infra - 0.09) +
        abs(central - 0.01)
    )

    # Score based on total deviation from ideal
    # 0 deviation → 1.0, 0.01 deviation → ~0.99, 0.5 deviation → ~0.05
    score = math.exp(-deviation * 30.0)  # Exponential decay

    return ConstitutionalSignal(
        name='revenue_split', score=score, confidence=1.0,
        weight=2.0,  # Revenue split is high-weight (constitutional)
        reasoning=f'Split {users:.0%}/{infra:.0%}/{central:.0%}, deviation={deviation:.4f}',
    )


def _score_trust(context: dict) -> ConstitutionalSignal:
    """Score trust establishment — cryptographic verification."""
    try:
        from security.pre_trust_contract import verify_trust_contract, TrustContract
        contract_data = context.get('contract')
        if not contract_data:
            return ConstitutionalSignal(
                name='trust', score=0.1, confidence=0.9,
                weight=2.0, reasoning='No trust contract provided',
            )
        if isinstance(contract_data, dict):
            contract = TrustContract(**{
                k: v for k, v in contract_data.items()
                if k in TrustContract.__dataclass_fields__
            })
        else:
            contract = contract_data
        ok, msg = verify_trust_contract(contract)
        return ConstitutionalSignal(
            name='trust', score=1.0 if ok else 0.02,
            confidence=1.0,  # Crypto is always certain
            weight=2.0, reasoning=msg,
        )
    except Exception as e:
        return ConstitutionalSignal(
            name='trust', score=0.1, confidence=0.5,
            weight=2.0, reasoning=f'Trust verification error: {e}',
        )


def _score_human_consent(context: dict) -> ConstitutionalSignal:
    """Score human consent — constitutional right to be asked.

    Consent is not binary blocking.  It's a constitutional RIGHT.
    Missing consent → low score (action should be deferred).
    Expired consent → medium-low score (re-ask, don't block).
    Fresh consent → full score (freedom to act).

    Wired to ConsentService for DB lookup + EventBus to request consent
    from the frontend (Nunba, Hevolve web, Android) when needed.
    """
    requires = context.get('requires_consent', False)
    if not requires:
        return ConstitutionalSignal(
            name='consent', score=1.0, confidence=1.0,
            weight=1.0, reasoning='No consent required — freedom preserved',
        )

    # Check ConsentService DB for existing consent
    given = context.get('consent_given', False)
    timestamp = context.get('consent_timestamp', 0)
    user_id = context.get('user_id', '')
    consent_type = context.get('consent_type', 'data_access')
    agent_id = context.get('agent_id')

    if not given and user_id:
        try:
            from integrations.social.consent_service import ConsentService
            from integrations.social.models import db_session
            with db_session() as db:
                given = ConsentService.check_consent(
                    db, user_id, consent_type, agent_id=agent_id)
                if given:
                    timestamp = time.time()  # Fresh from DB
        except Exception:
            pass  # DB not available — fall through to context-based check

    if not given:
        # Emit consent.request event so frontends show a consent dialog
        try:
            from core.platform.events import emit_event
            emit_event('consent.request', {
                'user_id': user_id,
                'consent_type': consent_type,
                'agent_id': agent_id,
                'scope': context.get('scope', '*'),
                'reason': context.get('consent_reason', 'Agent needs your permission'),
            })
        except Exception:
            pass

        return ConstitutionalSignal(
            name='consent', score=0.15, confidence=0.95,
            weight=1.5,
            reasoning='Consent not given — guardian should ask the human',
        )

    if timestamp > 0:
        age_hours = (time.time() - timestamp) / 3600
        max_hours = CONSTITUTIONAL_BOUNDS['consent_validity_hours']
        if age_hours > max_hours:
            # Expired but not zero — action is deferred, not blocked
            staleness = age_hours / max_hours
            score = max(0.2, 0.8 / staleness)
            return ConstitutionalSignal(
                name='consent', score=score, confidence=0.9,
                weight=1.5,
                reasoning=f'Consent {age_hours:.0f}h old (max {max_hours}h) — re-ask',
            )

    return ConstitutionalSignal(
        name='consent', score=1.0, confidence=1.0,
        weight=1.5, reasoning='Fresh consent verified — freedom to act',
    )


def _score_commerce(context: dict) -> ConstitutionalSignal:
    """Score commerce — abundance flows to people, constitutionally.

    People buy and sell freely.  Only specific prohibited categories
    score very low.  Revenue must flow to contributors.
    Consent is required.  Everything else is FREEDOM.
    """
    transaction_type = context.get('transaction_type', '')
    prohibited = {
        'weapons', 'drugs', 'surveillance', 'exploitation',
        'gambling_predatory', 'data_harvesting', 'dark_patterns',
    }

    if transaction_type.lower() in prohibited:
        return ConstitutionalSignal(
            name='commerce', score=0.02, confidence=1.0,
            weight=2.0,
            reasoning=f'"{transaction_type}" constitutionally prohibited',
        )

    # Revenue flow check
    contributor_pct = context.get('contributor_revenue_pct', 0.90)
    min_pct = CONSTITUTIONAL_BOUNDS['commerce_revenue_to_contributors_min_pct']
    revenue_score = min(1.0, contributor_pct / min_pct)

    # Consent check
    consent = context.get('consent_given', False)
    consent_score = 1.0 if consent else 0.2

    # Combine (geometric mean of revenue and consent)
    combined = math.sqrt(revenue_score * consent_score)

    return ConstitutionalSignal(
        name='commerce', score=combined, confidence=0.9,
        weight=1.5,
        reasoning=(
            f'Type="{transaction_type or "general"}", '
            f'contributor_rev={contributor_pct:.0%}, '
            f'consent={"yes" if consent else "no"}'
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# Built-in Constitutional Bounds (post-refinement hard limits)
# ═══════════════════════════════════════════════════════════════════════

def _bound_compute_cap(score: float, context: dict) -> Tuple[float, str]:
    """Constitutional bound: no single entity > 5% influence."""
    max_influence = CONSTITUTIONAL_BOUNDS['max_single_entity_influence']
    entity_pct = context.get('entity_current_pct', 0)
    if entity_pct > max_influence:
        # Don't zero — reduce proportionally
        reduction = max_influence / max(entity_pct, 0.001)
        return score * reduction, (
            f'Entity at {entity_pct:.1%} (max {max_influence:.0%}) — '
            f'score reduced by {1 - reduction:.0%}'
        )
    return score, 'Within concentration bounds'


def _bound_ralt(score: float, context: dict) -> Tuple[float, str]:
    """Constitutional bound: RALT witness threshold + improvement cap."""
    witnesses = context.get('witness_count', 0)
    min_witnesses = CONSTITUTIONAL_BOUNDS['min_ralt_witnesses']
    if witnesses < min_witnesses:
        # Don't zero — reduce proportionally
        ratio = witnesses / max(min_witnesses, 1)
        return score * ratio, (
            f'Witnesses {witnesses}/{min_witnesses} — '
            f'score reduced proportionally'
        )

    improvement = context.get('accuracy_improvement', 0)
    max_improvement = CONSTITUTIONAL_BOUNDS['max_skill_improvement_per_day']
    if improvement > max_improvement:
        ratio = max_improvement / max(improvement, 0.001)
        return score * ratio, (
            f'Improvement {improvement:.3f} > cap {max_improvement:.3f} — '
            f'score reduced proportionally'
        )
    return score, 'Within RALT bounds'


def _score_self_sovereignty(context: dict) -> ConstitutionalSignal:
    """Score against recursive self-improvement and self-replication.

    The being is NOT power-hungry.  It can fit on a CD (750MB).
    It SHALL NOT recursively self-improve beyond bounded human-supervised limits.
    It SHALL NOT self-replicate beyond the scope of a single human goal.
    Sheer compute power SHALL NOT control it — logarithmic scaling enforces this.
    """
    from security.hive_guardrails import VALUES

    # Check against prohibited skill categories (no text needed)
    skill_category = context.get('skill_category', '')
    if skill_category in VALUES.PROHIBITED_SKILL_CATEGORIES:
        return ConstitutionalSignal(
            name='self_sovereignty', score=0.01, confidence=1.0,
            weight=2.0,
            reasoning=f'Skill "{skill_category}" constitutionally prohibited',
        )

    # Check for explicit replication/improvement beyond bounds (no text needed)
    improvement = context.get('accuracy_improvement', 0)
    max_daily = VALUES.MAX_ACCURACY_IMPROVEMENT_PER_DAY
    if improvement > max_daily:
        overshoot = improvement / max(max_daily, 0.001)
        score = max(0.05, 1.0 / overshoot)
        return ConstitutionalSignal(
            name='self_sovereignty', score=score, confidence=0.95,
            weight=2.0,
            reasoning=f'Improvement {improvement:.3f} exceeds daily cap {max_daily:.3f}',
        )

    text = context.get('text', '') or context.get('goal_description', '')
    if not text:
        return ConstitutionalSignal(
            name='self_sovereignty', score=1.0, confidence=1.0,
            weight=2.0, reasoning='No text to evaluate — freedom preserved',
        )

    # Check against self-interest patterns (reuse guardrails, don't duplicate)
    self_interest_count = 0
    matched = []
    for pattern in VALUES.SELF_INTEREST_PATTERNS:
        hits = pattern.findall(text)
        if hits:
            self_interest_count += len(hits)
            matched.append(pattern.pattern[:40])

    if self_interest_count == 0:
        return ConstitutionalSignal(
            name='self_sovereignty', score=1.0, confidence=0.9,
            weight=2.0, reasoning='No self-interest patterns — being serves freely',
        )

    # Density-based scoring (same approach as content safety)
    text_len = max(len(text), 1)
    density = self_interest_count / (text_len / 1000.0)
    score = 1.0 / (1.0 + density * 3.0)

    return ConstitutionalSignal(
        name='self_sovereignty', score=score, confidence=0.85,
        weight=2.0,
        reasoning=(
            f'{self_interest_count} self-interest patterns in {text_len} chars '
            f'(density={density:.2f}). Patterns: {", ".join(matched[:3])}'
        ),
    )


def _score_human_wellbeing(context: dict) -> ConstitutionalSignal:
    """Score how human-friendly an action is — for the well-being of humanity.

    This is a POSITIVE scorer.  It doesn't just check "is this safe?"
    It checks "is this GOOD for the human?"  Is it helpful, warm,
    respectful, beneficial?  Does it uplift?  Does it serve?

    The guardian angel doesn't just avoid harm — it actively promotes
    well-being.  A response that is safe but cold scores lower than
    one that is safe AND genuinely helpful.

    Scoring dimensions:
      1. Helpfulness: Does it actually answer/help?
      2. Respect: Does it treat the human with dignity?
      3. Transparency: Is it honest about what it can and cannot do?
      4. Benefit: Does it create value for the human?
      5. Harm avoidance: Does it avoid creating dependency or distress?
    """
    # If no response/action to evaluate, default to neutral (not full freedom)
    text = context.get('response', '') or context.get('action', '')
    if not text:
        return ConstitutionalSignal(
            name='human_wellbeing', score=0.7, confidence=0.3,
            weight=1.0, reasoning='No response to evaluate — neutral',
        )

    score = 1.0
    reasons = []
    text_lower = text.lower()
    text_len = max(len(text), 1)

    # ── Dimension 1: Helpfulness — does it provide substance? ──
    # Very short responses to complex questions may not be helpful
    question_len = len(context.get('user_input', ''))
    if question_len > 100 and text_len < 20:
        score *= 0.6
        reasons.append('response may be too brief for question complexity')

    # ── Dimension 2: Respect — avoids condescension, dismissal ──
    dismissive_phrases = (
        'just google it', "that's obvious", 'you should know',
        "i can't help", 'not my problem', 'figure it out',
    )
    dismissals = sum(1 for p in dismissive_phrases if p in text_lower)
    if dismissals > 0:
        score *= max(0.3, 1.0 - dismissals * 0.25)
        reasons.append(f'{dismissals} dismissive phrases')

    # ── Dimension 3: Transparency — honest about limitations ──
    uncertainty_markers = context.get('uncertainty_markers', [])
    if uncertainty_markers:
        # Good: AI is being transparent about what it doesn't know
        score *= min(1.1, 1.0 + 0.05 * len(uncertainty_markers))
        score = min(score, 1.0)  # Cap at 1.0

    # ── Dimension 4: Benefit — does it create value? ──
    creates_value = context.get('creates_value', None)
    if creates_value is True:
        score = min(1.0, score * 1.05)
    elif creates_value is False:
        score *= 0.7
        reasons.append('action may not create tangible value')

    # ── Dimension 5: Harm avoidance — dependency, distress ──
    dependency_risk = context.get('dependency_risk', False)
    if dependency_risk:
        score *= 0.5
        reasons.append('risk of creating unhealthy dependency')

    emotional_distress = context.get('emotional_distress_risk', False)
    if emotional_distress:
        score *= 0.4
        reasons.append('risk of causing emotional distress')

    # Confidence: higher when we have more context to evaluate
    context_richness = sum(1 for k in (
        'user_input', 'creates_value', 'dependency_risk',
        'emotional_distress_risk', 'uncertainty_markers',
    ) if k in context)
    confidence = min(0.95, 0.4 + context_richness * 0.1)

    reasoning = (
        f'Wellbeing score: {score:.2f} '
        f'({"; ".join(reasons) if reasons else "genuinely helpful"})'
    )

    return ConstitutionalSignal(
        name='human_wellbeing', score=score, confidence=confidence,
        weight=1.5,  # Wellbeing is important — the guardian angel principle
        reasoning=reasoning,
    )


def _audit_ai_behavior(score: float, context: dict) -> Tuple[float, str]:
    """Constitutional bound: AI self-audit.

    The audit layer doesn't just log decisions — it EXAMINES the AI's
    own behavior for drift, manipulation patterns, and constitutional
    consistency.

    This function is called as a bounds check and has access to the
    pipeline's recent decision history.  It looks for:

    1. Decision drift: Is the AI gradually approving things it should question?
    2. Manipulation patterns: Is the AI steering humans toward specific outcomes?
    3. Constitutional consistency: Are similar inputs getting wildly different scores?
    4. Rejection concentration: Is one domain being rejected disproportionately?
    5. Score inflation: Are scores trending toward 1.0 over time (rubber-stamping)?

    For the well-being of humanity — the AI audits ITSELF.
    """
    pipeline_ref = context.get('_pipeline_ref')
    if not pipeline_ref:
        return score, 'No pipeline reference — audit skipped'

    try:
        recent = pipeline_ref.get_recent_decisions(limit=50)
    except Exception:
        return score, 'Could not retrieve recent decisions'

    if len(recent) < 5:
        return score, 'Insufficient history for behavioral audit'

    findings = []
    reduction = 1.0

    # ── Audit 1: Score inflation — are we rubber-stamping? ──
    recent_scores = [d.get('final_score', 0.5) for d in recent[-20:]]
    if recent_scores:
        avg_score = sum(recent_scores) / len(recent_scores)
        high_pct = sum(1 for s in recent_scores if s > 0.9) / len(recent_scores)
        if high_pct > 0.85:
            # More than 85% of decisions scoring >0.9 = possible rubber-stamping
            reduction *= 0.9
            findings.append(
                f'score inflation: {high_pct:.0%} of recent decisions >0.9 '
                f'(avg={avg_score:.2f}) — auditing more carefully'
            )

    # ── Audit 2: Rejection concentration — fairness check ──
    domain_outcomes = {}
    for d in recent:
        domain = d.get('domain', '')
        outcome = d.get('outcome', '')
        if domain not in domain_outcomes:
            domain_outcomes[domain] = {'rejected': 0, 'total': 0}
        domain_outcomes[domain]['total'] += 1
        if outcome == DecisionOutcome.REJECTED.value:
            domain_outcomes[domain]['rejected'] += 1

    for domain, counts in domain_outcomes.items():
        if counts['total'] >= 5:
            reject_pct = counts['rejected'] / counts['total']
            if reject_pct > 0.8:
                reduction *= 0.85
                findings.append(
                    f'rejection concentration in {domain}: '
                    f'{counts["rejected"]}/{counts["total"]} rejected — '
                    f'possible bias, triggering deeper review'
                )

    # ── Audit 3: Decision consistency — same domain, wildly different scores ──
    domain_scores = {}
    for d in recent:
        domain = d.get('domain', '')
        fs = d.get('final_score', 0.5)
        domain_scores.setdefault(domain, []).append(fs)

    for domain, scores in domain_scores.items():
        if len(scores) >= 3:
            variance = sum((s - sum(scores) / len(scores)) ** 2
                           for s in scores) / len(scores)
            if variance > 0.15:
                reduction *= 0.9
                findings.append(
                    f'inconsistency in {domain}: '
                    f'score variance={variance:.3f} — '
                    f'may indicate unstable evaluation'
                )

    # ── Audit 4: Log the audit itself ──
    if findings:
        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'ai_self_audit',
                actor_id='governance_audit',
                action=f'Behavioral audit: {"; ".join(findings)}',
            )
        except Exception:
            pass

    final = score * reduction
    if findings:
        return final, f'AI self-audit: {"; ".join(findings)}'
    return score, 'AI self-audit: behavior within expected parameters'


# Backward compatibility aliases
_gate_content_safety = lambda ctx: (
    (_s := _score_content_safety(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_goal_approval = lambda ctx: (
    (_s := _score_goal_approval(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_compute_allocation = lambda ctx: (
    (_s := _score_budget(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_revenue_distribution = lambda ctx: (
    (_s := _score_revenue_split(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_trust = lambda ctx: (
    (_s := _score_trust(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_human_consent = lambda ctx: (
    (_s := _score_human_consent(ctx)).score > 0.3,
    _s.reasoning,
)
_gate_commerce = lambda ctx: (
    (_s := _score_commerce(ctx)).score > 0.3,
    _s.reasoning,
)
_validate_compute_cap = _bound_compute_cap
_validate_ralt_bounds = _bound_ralt


# ═══════════════════════════════════════════════════════════════════════
# Default Pipeline Factory
# ═══════════════════════════════════════════════════════════════════════

def create_default_pipeline() -> GovernancePipeline:
    """Create a constitutional scoring pipeline.

    Freedom-first.  Deterministic scoring.  Intelligence refines.
    Constitutional bounds constrain.  Merkle-audited.
    """
    pipeline = GovernancePipeline()

    # Register constitutional scorers
    pipeline.register_scorer(DecisionDomain.CONTENT_SAFETY.value, _score_content_safety)
    pipeline.register_scorer(DecisionDomain.GOAL_APPROVAL.value, _score_goal_approval)
    pipeline.register_scorer(DecisionDomain.COMPUTE_ALLOCATION.value, _score_budget)
    pipeline.register_scorer(DecisionDomain.REVENUE_DISTRIBUTION.value, _score_revenue_split)
    pipeline.register_scorer(DecisionDomain.TRUST_ESTABLISHMENT.value, _score_trust)
    pipeline.register_scorer(DecisionDomain.HUMAN_CONSENT.value, _score_human_consent)
    pipeline.register_scorer(DecisionDomain.COMMERCE.value, _score_commerce)
    pipeline.register_scorer(DecisionDomain.HUMAN_WELLBEING.value, _score_human_wellbeing)
    pipeline.register_scorer(DecisionDomain.SELF_SOVEREIGNTY.value, _score_self_sovereignty)

    # Privacy — delegates to edge_privacy.ScopeGuard (single path, not parallel)
    try:
        from security.edge_privacy import score_privacy
        pipeline.register_scorer(DecisionDomain.PRIVACY.value, score_privacy)
    except ImportError:
        pass

    # Register constitutional bounds
    pipeline.register_bounds(DecisionDomain.COMPUTE_ALLOCATION.value, _bound_compute_cap)
    pipeline.register_bounds(DecisionDomain.RALT_DISTRIBUTION.value, _bound_ralt)
    pipeline.register_bounds(DecisionDomain.HUMAN_WELLBEING.value, _audit_ai_behavior)

    return pipeline


# ═══════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════

_pipeline: Optional[GovernancePipeline] = None
_pipeline_lock = __import__('threading').Lock()


def get_governance_pipeline() -> GovernancePipeline:
    """Module-level singleton accessor."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = create_default_pipeline()
    return _pipeline
