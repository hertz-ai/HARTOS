"""
Agent Attribution — Dense feedback with long-horizon credit assignment.

THE SINGLE CONVERGING PATH for agent action attribution.

All agents (daemon, benchmark_prover, compute_optimizer, hive_session,
task_protocol) call THIS module to record their actions. This module
routes ALL attribution to the existing WorldModelBridge — no parallel
systems, no new storage, no duplicate APIs.

How it works:
  1. Agent calls begin_action(goal_id, agent_id, action_type, expected_outcome)
     → returns action_id (correlation ID)
  2. Agent calls record_step(action_id, step_description, state, decision)
     at each intermediate state (every tick, every sub-decision)
  3. Agent calls complete_action(action_id, outcome)
     → computes attribution chain, submits to WorldModelBridge,
       compares vs expected_outcome for credit assignment

The WorldModelBridge.record_interaction() already accepts goal_id and
queues experiences for HevolveAI learning. We just wire the attribution
chain in the 'prompt' field as structured JSON.

For long-horizon goals (hours/days), the action stays open with step
history in memory (bounded by ACTION_TTL_SECONDS). On complete_action,
the full chain is submitted in a single experience with credit weights.

NO PARALLEL PATHS:
  - Storage: WorldModelBridge._experience_queue (existing)
  - Events: core.platform.events.emit_event (existing)
  - Privacy: security.secret_redactor (existing, applied by bridge)
  - Guardrails: security.hive_guardrails (existing, applied by bridge)
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.agent_attribution')

# ─── Config ─────────────────────────────────────────────────────────────

# How long an action stays open before auto-completion with outcome='timeout'.
# Set long enough for benchmark runs (hours) but bounded.
ACTION_TTL_SECONDS = 6 * 3600  # 6 hours

# Max open actions before oldest is force-completed (prevents unbounded growth).
MAX_OPEN_ACTIONS = 500

# Max steps per action (prevents DoS from tick storms).
MAX_STEPS_PER_ACTION = 1000


# ─── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class ActionStep:
    """A single intermediate state in an action chain."""
    timestamp: float
    description: str
    state: Dict[str, Any] = field(default_factory=dict)
    decision: str = ''  # What the agent decided at this step
    confidence: float = 0.5  # Agent's confidence in the decision

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AgentAction:
    """A long-horizon action with intermediate steps and expected outcome."""
    action_id: str
    goal_id: Optional[str]
    agent_id: str
    action_type: str  # e.g., 'benchmark_run', 'task_dispatch', 'optimization_cycle'
    started_at: float
    expected_outcome: Dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: List[str] = field(default_factory=list)
    steps: List[ActionStep] = field(default_factory=list)
    completed: bool = False
    outcome: Optional[Dict[str, Any]] = None
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            'steps': [s.to_dict() for s in self.steps],
        }


# ─── Orchestrator ───────────────────────────────────────────────────────

class AgentAttributionOrchestrator:
    """Single source of truth for agent action attribution.

    Agents call begin/record/complete — this class routes everything
    through the existing WorldModelBridge. No parallel storage.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._open_actions: Dict[str, AgentAction] = {}
        self._stats = {
            'total_begun': 0,
            'total_completed': 0,
            'total_timed_out': 0,
            'total_steps_recorded': 0,
        }

    # ─── Public API ─────────────────────────────────────────────────

    def begin_action(self, agent_id: str, action_type: str,
                     goal_id: Optional[str] = None,
                     expected_outcome: Optional[Dict] = None,
                     acceptance_criteria: Optional[List[str]] = None) -> str:
        """Start tracking a new action. Returns correlation action_id.

        Args:
            agent_id: Which agent is acting (e.g., 'agent_daemon', 'benchmark_prover')
            action_type: Category (e.g., 'benchmark_run', 'optimization_cycle')
            goal_id: Optional goal this action serves (for attribution)
            expected_outcome: Dict of {metric: expected_value} for comparison
            acceptance_criteria: List of string criteria for success

        Returns:
            action_id (UUID) — pass this to record_step and complete_action.
        """
        action_id = str(uuid.uuid4())
        action = AgentAction(
            action_id=action_id,
            goal_id=goal_id,
            agent_id=agent_id,
            action_type=action_type,
            started_at=time.time(),
            expected_outcome=expected_outcome or {},
            acceptance_criteria=acceptance_criteria or [],
        )

        evicted_action = None
        with self._lock:
            # Evict oldest if at cap (force-complete with timeout outcome)
            if len(self._open_actions) >= MAX_OPEN_ACTIONS:
                evicted_action = self._force_timeout_oldest()

            self._open_actions[action_id] = action
            self._stats['total_begun'] += 1

        # Submit evicted action to WMB OUTSIDE the lock (nested locks risk)
        if evicted_action is not None:
            try:
                self._submit_to_world_model(evicted_action)
                self._emit_completion_event(evicted_action)
            except Exception as exc:
                logger.debug("Evicted action submit failed: %s", exc)

        logger.debug(
            "Attribution: begin_action %s agent=%s type=%s goal=%s",
            action_id[:8], agent_id, action_type, goal_id,
        )
        return action_id

    def record_step(self, action_id: str, description: str,
                    state: Optional[Dict] = None,
                    decision: str = '',
                    confidence: float = 0.5) -> bool:
        """Record an intermediate state in the action chain.

        Called at every tick, every sub-decision, every state change.
        Dense recording = better attribution on completion.

        Returns:
            True if recorded, False if action unknown/completed/full.
        """
        with self._lock:
            action = self._open_actions.get(action_id)
            if action is None or action.completed:
                return False
            if len(action.steps) >= MAX_STEPS_PER_ACTION:
                return False

            step = ActionStep(
                timestamp=time.time(),
                description=description[:500],
                state=self._truncate_dict(state or {}),
                decision=decision[:200],
                confidence=max(0.0, min(1.0, confidence)),
            )
            action.steps.append(step)
            self._stats['total_steps_recorded'] += 1

        return True

    def complete_action(self, action_id: str,
                        outcome: Optional[Dict] = None) -> bool:
        """Complete an action. Submits full chain to WorldModelBridge.

        This is the long-horizon attribution moment:
          1. Compute credit assignment across steps (later steps closer to outcome
             get higher weight — exponential decay)
          2. Compare outcome vs expected_outcome for success/failure signal
          3. Submit chain + attribution + credit to WorldModelBridge for learning

        Returns:
            True if completed, False if action unknown.
        """
        with self._lock:
            action = self._open_actions.pop(action_id, None)
            if action is None:
                return False

            action.completed = True
            action.outcome = outcome or {}
            action.completed_at = time.time()
            self._stats['total_completed'] += 1

        self._submit_to_world_model(action)
        self._emit_completion_event(action)
        return True

    def get_action(self, action_id: str) -> Optional[AgentAction]:
        """Query a live or recently-completed action."""
        with self._lock:
            return self._open_actions.get(action_id)

    def get_stats(self) -> Dict:
        """Stats for observability/health checks."""
        with self._lock:
            return {
                **self._stats,
                'open_count': len(self._open_actions),
            }

    def cleanup_expired(self) -> int:
        """Force-complete actions older than TTL. Returns count timed out.

        Called periodically by the agent daemon tick.
        complete_action handles submit + event emission + total_completed.
        We additionally tag these as timed_out for stats distinction.
        """
        now = time.time()
        expired_ids = []
        with self._lock:
            for aid, action in self._open_actions.items():
                if now - action.started_at > ACTION_TTL_SECONDS:
                    expired_ids.append(aid)

        timed_out = 0
        for aid in expired_ids:
            if self.complete_action(aid, outcome={'status': 'timeout'}):
                timed_out += 1

        if timed_out:
            with self._lock:
                self._stats['total_timed_out'] += timed_out

        return timed_out

    # ─── Internal: attribution + WorldModelBridge routing ──────────

    def _submit_to_world_model(self, action: AgentAction) -> None:
        """Submit the completed action chain to WorldModelBridge.

        Uses the EXISTING record_interaction API — goal_id field is
        first-class. Attribution chain goes in the prompt field as
        structured JSON so HevolveAI can parse credit assignments.
        """
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
        except ImportError:
            logger.debug("WorldModelBridge unavailable, skipping attribution submit")
            return
        except Exception as exc:
            logger.debug("WorldModelBridge init failed: %s", exc)
            return

        # Compute credit assignment: exponential decay from outcome backward.
        # Later steps (closer to outcome) get higher weight.
        credits = self._compute_credit_assignment(action)

        # Compare outcome vs expected for success signal
        success_score = self._compute_success_score(action)

        # Build structured experience — uses existing record_interaction schema
        chain_summary = {
            'action_id': action.action_id,
            'agent_id': action.agent_id,
            'action_type': action.action_type,
            'goal_id': action.goal_id,
            'expected_outcome': action.expected_outcome,
            'acceptance_criteria': action.acceptance_criteria,
            'duration_seconds': round(
                (action.completed_at or time.time()) - action.started_at, 2),
            'step_count': len(action.steps),
            'outcome': action.outcome,
            'success_score': success_score,
            'step_credits': credits,
            'steps_summary': [
                {
                    'desc': s.description,
                    'decision': s.decision,
                    'confidence': s.confidence,
                    'credit': credits.get(i, 0.0),
                }
                for i, s in enumerate(action.steps[-50:])  # last 50 steps
            ],
        }

        try:
            bridge.record_interaction(
                user_id=action.agent_id,
                prompt_id=action.action_id,
                prompt=json.dumps(chain_summary, default=str)[:2000],
                response=json.dumps(action.outcome or {}, default=str)[:5000],
                model_id=f'{action.agent_id}:{action.action_type}',
                latency_ms=(action.completed_at - action.started_at) * 1000 if action.completed_at else 0,
                goal_id=action.goal_id,
            )
        except Exception as exc:
            logger.debug("record_interaction failed: %s", exc)

    def _compute_credit_assignment(self, action: AgentAction) -> Dict[int, float]:
        """Assign credit to each step for the final outcome.

        Uses exponential decay: step at position i gets weight 0.9^(n-i).
        Later steps (closer to outcome) get more credit.

        Returns:
            Dict mapping step_index → credit weight (sum ≈ 1.0).
        """
        n = len(action.steps)
        if n == 0:
            return {}

        decay = 0.9
        raw_weights = {i: decay ** (n - 1 - i) for i in range(n)}
        total = sum(raw_weights.values())
        if total <= 0:
            return {i: 1.0 / n for i in range(n)}
        return {i: round(w / total, 4) for i, w in raw_weights.items()}

    def _compute_success_score(self, action: AgentAction) -> float:
        """Compare outcome vs expected_outcome. Returns [0.0, 1.0].

        - If expected_outcome is empty: return 0.5 (neutral)
        - If outcome has 'error' / 'status' == 'error': return 0.0
        - Otherwise: fraction of expected keys matched within tolerance.
        """
        outcome = action.outcome or {}
        if not action.expected_outcome:
            # No expectation set — neutral score, use outcome status
            if outcome.get('status') == 'error' or outcome.get('error'):
                return 0.0
            if outcome.get('status') == 'timeout':
                return 0.2
            return 0.5

        if outcome.get('status') == 'error' or outcome.get('error'):
            return 0.0

        # Check expected keys
        matched = 0
        total = 0
        for key, expected in action.expected_outcome.items():
            total += 1
            actual = outcome.get(key)
            if actual is None:
                continue
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                # Numeric: within 10% tolerance (relative) OR 0.05 absolute for small values.
                # The absolute band catches cases like expected=0 + actual=0.03 (delta
                # metrics), where pure relative tolerance would fail every near-zero case.
                if abs(actual - expected) <= 0.05:
                    matched += 1
                elif expected != 0 and abs(actual - expected) / abs(expected) <= 0.1:
                    matched += 1
            elif actual == expected:
                matched += 1

        return matched / total if total > 0 else 0.5

    def _emit_completion_event(self, action: AgentAction) -> None:
        """Emit EventBus event for real-time dashboards."""
        try:
            from core.platform.events import emit_event
            emit_event('agent.action.completed', {
                'action_id': action.action_id,
                'agent_id': action.agent_id,
                'action_type': action.action_type,
                'goal_id': action.goal_id,
                'duration_seconds': round(
                    (action.completed_at or time.time()) - action.started_at, 2),
                'step_count': len(action.steps),
                'success_score': self._compute_success_score(action),
            })
        except Exception:
            pass  # Best effort — never crash on observability

    def _force_timeout_oldest(self) -> Optional[AgentAction]:
        """Force-complete the oldest open action when at cap.

        Caller MUST hold _lock. Returns the evicted action so the caller
        can submit it to WorldModelBridge OUTSIDE the lock (avoids nested
        lock acquisition on WMB's internal lock).
        """
        if not self._open_actions:
            return None
        oldest_id = min(
            self._open_actions.keys(),
            key=lambda k: self._open_actions[k].started_at,
        )
        oldest = self._open_actions.pop(oldest_id)
        oldest.completed = True
        oldest.outcome = {'status': 'evicted', 'reason': 'max_open_actions'}
        oldest.completed_at = time.time()
        self._stats['total_timed_out'] += 1
        return oldest

    @staticmethod
    def _truncate_dict(d: Dict) -> Dict:
        """Truncate string values in dict to prevent unbounded state growth."""
        out = {}
        for k, v in d.items():
            if isinstance(v, str) and len(v) > 500:
                out[k] = v[:500] + '...'
            elif isinstance(v, (dict, list)) and len(str(v)) > 1000:
                out[k] = str(v)[:1000] + '...'
            else:
                out[k] = v
        return out


# ─── Singleton ──────────────────────────────────────────────────────────

_orchestrator: Optional[AgentAttributionOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_attribution() -> AgentAttributionOrchestrator:
    """Get the singleton orchestrator. Thread-safe."""
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                _orchestrator = AgentAttributionOrchestrator()
    return _orchestrator


# ─── Convenience functions (thin wrappers for call-site brevity) ───────

def begin_action(agent_id: str, action_type: str,
                 goal_id: Optional[str] = None,
                 expected_outcome: Optional[Dict] = None,
                 acceptance_criteria: Optional[List[str]] = None) -> str:
    """Convenience: start tracking. See AgentAttributionOrchestrator.begin_action."""
    return get_attribution().begin_action(
        agent_id, action_type, goal_id, expected_outcome, acceptance_criteria,
    )


def record_step(action_id: str, description: str,
                state: Optional[Dict] = None,
                decision: str = '',
                confidence: float = 0.5) -> bool:
    """Convenience: record intermediate state."""
    return get_attribution().record_step(
        action_id, description, state, decision, confidence,
    )


def complete_action(action_id: str, outcome: Optional[Dict] = None) -> bool:
    """Convenience: complete action and submit attribution."""
    return get_attribution().complete_action(action_id, outcome)
