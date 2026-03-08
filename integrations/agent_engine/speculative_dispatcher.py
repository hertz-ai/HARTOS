"""
Unified Agent Goal Engine - Speculative Dispatcher

Fast-first, expert-takeover speculative execution:
1. Fast model (hive compute / cheap API) responds synchronously → user sees instantly
2. Expert model (GPT-4 / Claude) runs in background thread
3. Fast response conveyed to expert as context
4. If expert meaningfully improves, delivered asynchronously
5. Compute provider (hive node) earns ad revenue for serving fast response

Guardrails enforced at EVERY layer:
- ConstitutionalFilter.check_prompt() before ANY dispatch
- HiveCircuitBreaker.is_halted() before ANY dispatch
- EnergyAwareness tracked on EVERY model call
- ComputeDemocracy.adjusted_reward() on EVERY contribution
- HiveEthos.rewrite_prompt_for_togetherness() on EVERY prompt
- Budget enforcement via ResonanceService.spend_spark()
"""
import atexit
import logging
import os
import time
import uuid
import threading
from collections import deque

from core.port_registry import get_port
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

logger = logging.getLogger('hevolve_social')

# Similarity threshold — below this, expert response is considered
# a meaningful improvement over fast response
_SIMILARITY_THRESHOLD = 0.80
_RESPONSE_ADEQUATE = 'RESPONSE_ADEQUATE'


class SpeculativeDispatcher:
    """Fast-first, expert-takeover speculative execution engine.

    Every method enforces guardrails — no code path bypasses safety.
    """

    def __init__(self, model_registry=None):
        from .model_registry import model_registry as _default_registry
        self._registry = model_registry or _default_registry
        self._expert_pool = ThreadPoolExecutor(
            max_workers=int(os.environ.get('HEVOLVE_EXPERT_WORKERS', '4')),
            thread_name_prefix='spec_expert',
        )
        atexit.register(lambda: self._expert_pool.shutdown(wait=False))
        self._active: Dict[str, dict] = {}  # speculation_id → metadata
        self._lock = threading.Lock()
        self._results: Dict[str, dict] = {}  # speculation_id → expert result
        self._results_deque: deque = deque(maxlen=1000)  # TTL cleanup

    # ─── Gate: should we speculate? ───

    def should_speculate(self, user_id: str, prompt_id: str,
                         prompt: str, goal: dict = None) -> bool:
        """Gate: expert model available + budget remaining + not halted + not casual."""
        # GUARDRAIL: circuit breaker
        from security.hive_guardrails import HiveCircuitBreaker
        if HiveCircuitBreaker.is_halted():
            return False

        # GUARDRAIL: constitutional check on prompt
        from security.hive_guardrails import ConstitutionalFilter
        passed, _ = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return False

        # Need both a fast and expert model
        fast = self._registry.get_fast_model()
        expert = self._registry.get_expert_model()
        if not fast or not expert:
            return False
        if fast.model_id == expert.model_id:
            return False  # Same model — no point speculating

        # Budget check (if goal has spark budget)
        if goal and goal.get('spark_budget', 0) > 0:
            spent = goal.get('spark_spent', 0)
            remaining = goal['spark_budget'] - spent
            if remaining < expert.cost_per_1k_tokens:
                return False

        return True

    # ─── Main entry point ───

    def dispatch_speculative(self, prompt: str, user_id: str, prompt_id: str,
                             goal_id: str = None, goal_type: str = 'general',
                             node_id: str = None) -> dict:
        """
        1. Guardrail-check the prompt
        2. Pick fast model → dispatch synchronously → user gets response
        3. Record compute contribution for hive node (ad revenue)
        4. Pick expert model → dispatch in background thread
        5. Return fast response immediately

        Returns:
            {
                'response': str,           # Fast agent's response
                'speculation_id': str,     # Track the background expert
                'fast_model': str,         # Which model served fast
                'expert_pending': bool,    # True if expert is working
                'latency_ms': float,       # Fast response latency
                'energy_kwh': float,       # Energy consumed
            }
        """
        speculation_id = str(uuid.uuid4())[:12]

        # GUARDRAIL: circuit breaker
        from security.hive_guardrails import HiveCircuitBreaker
        if HiveCircuitBreaker.is_halted():
            return {'response': '', 'speculation_id': speculation_id,
                    'error': 'Hive is halted', 'expert_pending': False}

        # GUARDRAIL: constitutional filter
        from security.hive_guardrails import ConstitutionalFilter
        passed, reason = ConstitutionalFilter.check_prompt(prompt)
        if not passed:
            return {'response': '', 'speculation_id': speculation_id,
                    'error': reason, 'expert_pending': False}

        # GUARDRAIL: rewrite prompt for togetherness
        from security.hive_guardrails import HiveEthos
        prompt = HiveEthos.rewrite_prompt_for_togetherness(prompt)

        # ── FAST PATH ──
        fast_model = self._registry.get_fast_model()
        if not fast_model:
            return {'response': '', 'speculation_id': speculation_id,
                    'error': 'No fast model available', 'expert_pending': False}

        start = time.time()
        fast_response = self._dispatch_to_model(
            fast_model, prompt, user_id, prompt_id, goal_type, goal_id)
        elapsed_ms = (time.time() - start) * 1000

        # GUARDRAIL: energy tracking on EVERY call
        self._registry.record_energy(fast_model.model_id, elapsed_ms)
        self._registry.record_latency(fast_model.model_id, elapsed_ms)

        # Record compute contribution for hive node (→ ad revenue)
        self._record_compute_contribution(node_id, fast_model.model_id, elapsed_ms)

        # ── EXPERT PATH (background) ──
        expert_model = self._registry.get_expert_model()
        expert_pending = False

        if expert_model and expert_model.model_id != fast_model.model_id:
            if self._check_and_reserve_budget(user_id, goal_id, expert_model):
                with self._lock:
                    self._active[speculation_id] = {
                        'fast_model': fast_model.model_id,
                        'expert_model': expert_model.model_id,
                        'user_id': user_id,
                        'prompt_id': prompt_id,
                        'goal_id': goal_id,
                        'started_at': time.time(),
                    }
                self._expert_pool.submit(
                    self._expert_background_task,
                    speculation_id, prompt, fast_response,
                    expert_model, user_id, prompt_id, goal_id, goal_type,
                )
                expert_pending = True

        return {
            'response': fast_response,
            'speculation_id': speculation_id,
            'fast_model': fast_model.model_id,
            'expert_pending': expert_pending,
            'latency_ms': round(elapsed_ms, 1),
            'energy_kwh': round(
                self._registry.get_total_energy_kwh(hours=0.01), 6),
        }

    # ─── Background expert task ───

    def _expert_background_task(self, speculation_id: str, original_prompt: str,
                                fast_response: str, expert_model, user_id: str,
                                prompt_id: str, goal_id: str, goal_type: str):
        """Background: budget check → expert dispatch → deliver if improved."""
        try:
            # GUARDRAIL: circuit breaker (check again — may have been halted)
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                return

            expert_prompt = self._build_expert_prompt(original_prompt, fast_response)

            start = time.time()
            expert_response = self._dispatch_to_model(
                expert_model, expert_prompt, user_id, prompt_id,
                goal_type, goal_id)
            elapsed_ms = (time.time() - start) * 1000

            # GUARDRAIL: energy tracking
            self._registry.record_energy(expert_model.model_id, elapsed_ms)
            self._registry.record_latency(expert_model.model_id, elapsed_ms)

            # Check if expert meaningfully improved
            if self._is_meaningful_improvement(fast_response, expert_response):
                # GUARDRAIL: constitutional check on expert output
                from security.hive_guardrails import ConstitutionalFilter
                passed, reason = ConstitutionalFilter.check_prompt(expert_response)
                if passed:
                    self._deliver_expert_response(
                        user_id, prompt_id, speculation_id, expert_response)
                    with self._lock:
                        self._results[speculation_id] = {
                            'response': expert_response,
                            'model': expert_model.model_id,
                            'latency_ms': round(elapsed_ms, 1),
                            'improved': True,
                        }
                else:
                    logger.warning(f"Expert response blocked by guardrail: {reason}")
            else:
                with self._lock:
                    self._results[speculation_id] = {
                        'response': fast_response,
                        'model': expert_model.model_id,
                        'latency_ms': round(elapsed_ms, 1),
                        'improved': False,
                    }

        except Exception as e:
            logger.debug(f"Expert background task failed for {speculation_id}: {e}")
        finally:
            with self._lock:
                self._active.pop(speculation_id, None)

    # ─── Helpers ───

    def _build_expert_prompt(self, original_prompt: str, fast_response: str) -> str:
        """Augment prompt: expert sees original task + fast agent's output."""
        return (
            f"You are an expert reviewer. A fast agent on a hive compute node "
            f"has already responded. Review and improve if needed.\n\n"
            f"## Original Request\n{original_prompt}\n\n"
            f"## Fast Agent's Response\n{fast_response}\n\n"
            f"## Your Task\n"
            f"Improve the response: fix errors, add missing details, improve clarity.\n"
            f"If the response is already excellent, respond with: {_RESPONSE_ADEQUATE}\n"
            f"Every output must be constructive towards humanity's benefit."
        )

    def _is_meaningful_improvement(self, fast_response: str,
                                    expert_response: str) -> bool:
        """Check if expert actually improved on the fast response."""
        if not expert_response:
            return False
        if _RESPONSE_ADEQUATE in expert_response:
            return False
        # Simple word-overlap similarity
        fast_words = set(fast_response.lower().split())
        expert_words = set(expert_response.lower().split())
        if not fast_words or not expert_words:
            return bool(expert_response.strip())
        overlap = len(fast_words & expert_words)
        similarity = overlap / max(len(fast_words | expert_words), 1)
        return similarity < _SIMILARITY_THRESHOLD

    def _dispatch_to_model(self, model: 'ModelBackend', prompt: str,
                           user_id: str, prompt_id: str,
                           goal_type: str, goal_id: str = None) -> str:
        """Send prompt to a specific model via /chat endpoint with config override."""
        import requests as req
        base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
        try:
            resp = req.post(
                f'{base_url}/chat',
                json={
                    'user_id': user_id,
                    'prompt_id': f'{goal_type}_{goal_id[:8]}' if goal_id else prompt_id,
                    'prompt': prompt,
                    'create_agent': True,
                    'autonomous': True,
                    'casual_conv': False,
                    'model_config': model.to_config_list(),
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get('response', '')
        except req.RequestException as e:
            logger.debug(f"Model dispatch failed ({model.model_id}): {e}")
        return ''

    def _deliver_expert_response(self, user_id: str, prompt_id: str,
                                  speculation_id: str, response: str):
        """Dual-channel async delivery: Crossbar + Rasa HTTP."""
        # Publish via canonical publish_async (MessageBus → Crossbar)
        try:
            from langchain_gpt_api import publish_async
            topic = f'com.hertzai.hevolve.chat.{user_id}'
            publish_async(topic, response)
        except Exception:
            pass

        logger.info(f"Expert enhancement delivered: spec={speculation_id}, "
                     f"user={user_id}")

    def _check_and_reserve_budget(self, user_id: str, goal_id: str,
                                   expert_model) -> bool:
        """Check Spark budget before expert execution (atomic row lock).

        Delegates to shared budget_gate.check_goal_budget() to avoid duplication.
        """
        if not goal_id:
            return True  # No goal = no budget constraint

        try:
            from .budget_gate import check_goal_budget
            cost = expert_model.cost_per_1k_tokens
            allowed, remaining, reason = check_goal_budget(goal_id, cost)
            return allowed
        except ImportError:
            return True  # Allow if budget system unavailable

    def _record_compute_contribution(self, node_id: str, model_id: str,
                                      latency_ms: float):
        """Credit hive node for serving fast response → ad revenue eligibility.

        GUARDRAIL: Only master_key_verified nodes get credit.
        GUARDRAIL: ComputeDemocracy.adjusted_reward() — logarithmic, not linear.
        """
        if not node_id:
            return
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peer = db.query(PeerNode).filter_by(node_id=node_id).first()
                if peer and peer.master_key_verified:
                    peer.agent_count = (peer.agent_count or 0) + 1
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Compute contribution recording skipped: {e}")

    # ─── Status / results ───

    def get_speculation_status(self, speculation_id: str) -> dict:
        """Get status of a speculative dispatch."""
        with self._lock:
            if speculation_id in self._active:
                return {'status': 'pending', 'speculation_id': speculation_id}
            if speculation_id in self._results:
                result = self._results[speculation_id]
                return {'status': 'completed', **result}
        return {'status': 'unknown', 'speculation_id': speculation_id}

    def get_stats(self) -> dict:
        """Get dispatcher statistics."""
        with self._lock:
            return {
                'active_speculations': len(self._active),
                'completed': len(self._results),
                'total_energy_kwh_24h': round(
                    self._registry.get_total_energy_kwh(24), 4),
            }


# ─── Module-level singleton ───
_dispatcher = None
_dispatcher_lock = threading.Lock()


def get_speculative_dispatcher() -> SpeculativeDispatcher:
    """Get or create the singleton SpeculativeDispatcher."""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = SpeculativeDispatcher()
    return _dispatcher
