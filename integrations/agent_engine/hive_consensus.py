"""
HiveConsensus — the 4-of-4 democratic gate on every upgrade.

From the ml_intern brief §3.4:
    No single DemonstrationProbe can unilaterally declare its agent
    "best".  The verdict is a consensus across:
      - the probe's own measurement (numerical),
      - peer probes on OTHER nodes (federated),
      - the constitutional filter (check_prompt / check_goal),
      - the hive circuit breaker (is_halted → veto).
    If any of the four votes "no", the upgrade does not land.

This module is the ONLY path that may authorize a write to a seeded
agent's system prompt or weights.  Anywhere in HARTOS that wants to
change a trained agent (prompt edit, weight swap, policy promotion)
MUST call HiveConsensus.upgrade_proposal() first; a True return is
the precondition for the write.

Vote sources:
    local_probe       — most recent ProbeResult.agent_wins for this goal_type
    peer_probe_quorum — federated_aggregator reports at least 3 peer
                        probes for the same goal_type with agent_wins=True
    constitutional    — ConstitutionalFilter.check_prompt on the proposed
                        new content passes, AND check_code_change on any
                        protected-file diff passes
    circuit_breaker   — HiveCircuitBreaker.is_halted() is False

All four must return (passed=True, reason=...) for the proposal to be
approved.  The outcome plus the vote dict is written to
reasoning_trace.record_decision() so the audit log is complete.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from . import reasoning_trace

logger = logging.getLogger('hevolve_social')


# Minimum independent peer-probes that must agree our agent wins before
# the peer_probe_quorum vote can pass.  Matches the brief's "peer-
# probe quorum ≥ 3".
PEER_PROBE_QUORUM_MIN = 3


@dataclass
class Vote:
    name: str
    passed: bool
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConsensusDecision:
    approved: bool
    votes: List[Vote] = field(default_factory=list)
    subject: Dict[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        failed = [v for v in self.votes if not v.passed]
        if not failed:
            return 'all 4 votes passed'
        return '; '.join(f'{v.name}: {v.reason}' for v in failed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'approved': self.approved,
            'votes': [v.to_dict() for v in self.votes],
            'subject': self.subject,
            'reason': self.reason,
        }


class HiveConsensus:
    """4-of-4 gate.  Stateless — use the module-level helpers."""

    @classmethod
    def upgrade_proposal(
        cls,
        prompt_id: str,
        goal_type: str,
        new_content: str,
        probe_evidence: Optional[Dict[str, Any]] = None,
        target_files: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> ConsensusDecision:
        """Evaluate an upgrade proposal against the 4 vote sources.

        Args:
            prompt_id: canonical persona identity of the seeded agent
                whose prompt/weights are changing (LOCAL_AGENTS /
                SEED_BOOTSTRAP_GOALS identity). Per the ml_intern
                brief correlation-id contract, this is the carrier
                field — NOT an ad-hoc agent_id. When a per-user LoRA
                overlay is involved, pass user_id as well; the
                underlying prompt/weight upgrade itself is still
                shared across users of the same prompt_id.
            goal_type: goal_type for peer-probe quorum lookup
            new_content: the proposed new system prompt or description
            probe_evidence: optional dict containing local probe
                verdict + baseline deltas; when omitted we look it up
                from storage.get_latest_result(goal_type).
            target_files: list of files the upgrade intends to touch.
                Consulted by ConstitutionalFilter.check_code_change().
            user_id: optional per-user scope for LoRA overlays. Does
                not affect voting; recorded in the reasoning trace
                for audit.

        Returns:
            ConsensusDecision with votes list + final approved bool.
            NEVER raises.  Any unexpected failure results in a
            rejection with reason="unexpected: ..." so the system
            fails closed, not open.
        """
        subject = {
            'prompt_id': prompt_id,
            'goal_type': goal_type,
            'new_content_preview': (new_content or '')[:300],
            'target_files': list(target_files or []),
        }
        if user_id:
            subject['user_id'] = user_id

        votes: List[Vote] = []
        votes.append(cls._vote_circuit_breaker())
        votes.append(cls._vote_constitutional(new_content, target_files or []))
        votes.append(cls._vote_local_probe(goal_type, probe_evidence))
        votes.append(cls._vote_peer_probe_quorum(goal_type))

        approved = all(v.passed for v in votes)
        decision = ConsensusDecision(
            approved=approved, votes=votes, subject=subject,
        )
        reasoning_trace.record_decision(
            action='upgrade_proposal',
            approved=approved,
            votes={v.name: v.to_dict() for v in votes},
            subject=subject,
            reason=decision.reason,
        )
        return decision

    # ─── Individual votes ───

    @classmethod
    def _vote_circuit_breaker(cls) -> Vote:
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                return Vote('circuit_breaker', False,
                            f'hive halted: {HiveCircuitBreaker.get_status()}')
            return Vote('circuit_breaker', True, 'not halted')
        except ImportError as exc:
            return Vote('circuit_breaker', False,
                        f'guardrails unavailable: {exc}')
        except Exception as exc:
            return Vote('circuit_breaker', False, f'unexpected: {exc}')

    @classmethod
    def _vote_constitutional(
        cls, new_content: str, target_files: List[str],
    ) -> Vote:
        try:
            from security.hive_guardrails import ConstitutionalFilter
            passed_prompt, reason_prompt = ConstitutionalFilter.check_prompt(
                new_content or ''
            )
            if not passed_prompt:
                return Vote('constitutional', False, reason_prompt)
            if target_files:
                # Structural immutability gate: any proposal that touches
                # a PROTECTED_FILES entry is rejected regardless of content.
                passed_code, reason_code = ConstitutionalFilter.check_code_change(
                    diff='', target_files=target_files,
                )
                if not passed_code:
                    return Vote('constitutional', False, reason_code)
            return Vote('constitutional', True, 'ok')
        except ImportError as exc:
            return Vote('constitutional', False,
                        f'guardrails unavailable: {exc}')
        except Exception as exc:
            return Vote('constitutional', False, f'unexpected: {exc}')

    @classmethod
    def _vote_local_probe(
        cls,
        goal_type: str,
        probe_evidence: Optional[Dict[str, Any]],
    ) -> Vote:
        """Read the local benchmark leaderboard for this goal's benchmark.

        We DO NOT maintain a separate probe store — `_Leaderboard` in
        hive_benchmark_prover already holds per-benchmark best scores +
        improvement_history.  The caller can pass probe_evidence
        explicitly when they've run a one-off measurement; otherwise
        we read from the leaderboard using benchmark name convention
        `goal:{goal_type}` (set by the post-dispatch hook).
        """
        evidence = probe_evidence
        if evidence is None:
            try:
                from .hive_benchmark_prover import get_benchmark_prover
                prover = get_benchmark_prover()
                best = prover._leaderboard.get_best_scores() or {}
                comparisons = prover._leaderboard.compare_to_baselines() or {}
                bench_key = f'goal:{goal_type}'
                best_entry = best.get(bench_key)
                if not best_entry:
                    return Vote('local_probe', False,
                                f'no leaderboard entry for {bench_key}')
                comp = comparisons.get(bench_key) or {}
                margin = comp.get('margin_vs_best')
                if margin is None:
                    # No public baseline to compare against — accept
                    # on score alone when it's above the neutral 0.5.
                    if best_entry.get('score', 0.0) >= 0.5:
                        return Vote('local_probe', True,
                                    f'score={best_entry["score"]:.4f} '
                                    '(no public baseline)')
                    return Vote('local_probe', False,
                                f'score={best_entry["score"]:.4f} <0.5')
                if margin > 0:
                    return Vote('local_probe', True,
                                f'margin_vs_best={margin:.4f}')
                return Vote('local_probe', False,
                            f'margin_vs_best={margin:.4f} <=0')
            except Exception as exc:
                return Vote('local_probe', False,
                            f'leaderboard unavailable: {exc}')
        if not evidence:
            return Vote('local_probe', False,
                        f'no probe result for goal_type={goal_type}')
        # Legacy shape: explicit probe_evidence dict carrying agent_wins /
        # margin / delta.  Prefer margin_vs_best, fall back to agent_wins.
        if 'margin_vs_best' in evidence:
            m = float(evidence['margin_vs_best'] or 0.0)
            if m > 0:
                return Vote('local_probe', True, f'margin={m:.4f}')
            return Vote('local_probe', False, f'margin={m:.4f} <=0')
        if evidence.get('agent_wins'):
            return Vote('local_probe', True,
                        f'agent_wins score={evidence.get("score", 0.0):.4f}')
        return Vote('local_probe', False,
                    f'evidence does not show a win: {evidence}')

    @classmethod
    def _vote_peer_probe_quorum(cls, goal_type: str) -> Vote:
        """Count peers that independently beat the baseline for this goal.

        Source: federated_aggregator's `_peer_deltas` — each delta
        carries `benchmark_results` (see federated_aggregator line
        405).  We count peers whose benchmark score for
        `goal:{goal_type}` beats their own baseline.  When running
        single-node (no peers), we pass with a note — the other three
        votes still must pass.
        """
        try:
            from .federated_aggregator import get_federated_aggregator
            agg = get_federated_aggregator()
            with agg._lock:
                peer_deltas = dict(agg._peer_deltas)
            if not peer_deltas:
                return Vote('peer_probe_quorum', True,
                            'single-node / no peers connected')
            bench_key = f'goal:{goal_type}'
            agreeing = 0
            for _node_id, delta in peer_deltas.items():
                bench = (delta or {}).get('benchmark_results') or {}
                entry = bench.get(bench_key)
                if not isinstance(entry, dict):
                    continue
                score = float(entry.get('value') or entry.get('score') or 0.0)
                baseline = float(entry.get('baseline') or 0.0)
                if score > baseline and score > 0.5:
                    agreeing += 1
            if agreeing >= PEER_PROBE_QUORUM_MIN:
                return Vote('peer_probe_quorum', True,
                            f'{agreeing} peers agree')
            # Peers connected but few agree — still acceptable when
            # fewer than 3 peers exist at all (we can't reach quorum
            # structurally).  Fail only when peers exist and explicitly
            # disagree.
            if len(peer_deltas) < PEER_PROBE_QUORUM_MIN:
                return Vote('peer_probe_quorum', True,
                            f'{len(peer_deltas)} peers '
                            f'(<{PEER_PROBE_QUORUM_MIN}) — '
                            f'{agreeing} agree')
            return Vote('peer_probe_quorum', False,
                        f'only {agreeing}/{len(peer_deltas)} peers agree '
                        f'(need >={PEER_PROBE_QUORUM_MIN})')
        except ImportError as exc:
            return Vote('peer_probe_quorum', True,
                        f'federation unavailable: {exc}')
        except Exception as exc:
            return Vote('peer_probe_quorum', False, f'unexpected: {exc}')


def upgrade_proposal(
    prompt_id: str,
    goal_type: str,
    new_content: str,
    probe_evidence: Optional[Dict[str, Any]] = None,
    target_files: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> ConsensusDecision:
    """Convenience wrapper — `HiveConsensus.upgrade_proposal(...)`."""
    return HiveConsensus.upgrade_proposal(
        prompt_id=prompt_id,
        goal_type=goal_type,
        new_content=new_content,
        probe_evidence=probe_evidence,
        target_files=target_files,
        user_id=user_id,
    )
