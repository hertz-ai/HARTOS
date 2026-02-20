"""
Distributed Worker Loop — auto-claim and execute tasks on worker nodes.

Runs as a background daemon thread on every node where the shared Redis
coordinator is reachable. No separate mode flag — if Redis exists and
this node is part of a hive, the worker loop auto-starts and claims tasks.

Polls the shared DistributedTaskCoordinator for unclaimed tasks,
executes them via the local /chat endpoint, and submits results back.
"""
import os
import time
import logging
import threading
import requests
from typing import Optional

logger = logging.getLogger('hevolve_social')


class DistributedWorkerLoop:
    """Background loop: claim tasks from shared Redis, execute via local /chat, submit results."""

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_WORKER_POLL_INTERVAL', '15'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._node_id = os.environ.get('HEVOLVE_NODE_ID', 'unknown')
        self._capabilities = self._detect_capabilities()

    def _detect_capabilities(self):
        """Detect this node's capabilities from system_requirements."""
        caps = ['marketing', 'news', 'finance', 'revenue']  # Base capabilities
        try:
            from security.system_requirements import get_capabilities
            hw = get_capabilities()
            if hw:
                tier = hw.tier.value
                if tier in ('standard', 'performance', 'compute_host'):
                    caps.extend(['coding', 'ip_protection', 'provision'])
                if tier in ('performance', 'compute_host'):
                    caps.append('vision')
        except Exception:
            pass
        return caps

    def start(self):
        """Start the worker loop if a shared Redis coordinator is reachable.

        No separate mode flag — if Redis is available, the worker loop
        starts and will claim tasks from the shared queue. This is how
        a node joins the distributed hive: just have Redis reachable.
        """
        if not self._is_enabled():
            logger.debug("Distributed worker loop: Redis coordinator not reachable, skipping")
            return

        with self._lock:
            if self._running:
                return
            self._running = True

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Distributed worker loop started (interval={self._interval}s, "
                    f"capabilities={self._capabilities})")

    def stop(self):
        """Stop the worker loop."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    @staticmethod
    def _is_enabled() -> bool:
        """Check if the shared coordinator is reachable (Redis available).

        Uses the existing tier system — no separate distributed mode flag.
        """
        try:
            from integrations.distributed_agent.api import _get_coordinator
            coord = _get_coordinator()
            return coord is not None
        except Exception:
            return False

    def _loop(self):
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('distributed_worker')
            except Exception:
                pass
            # GUARDRAIL: circuit breaker
            try:
                from security.hive_guardrails import HiveCircuitBreaker
                if HiveCircuitBreaker.is_halted():
                    continue
            except ImportError:
                pass
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"Distributed worker tick error: {e}")

    def _tick(self):
        """Try to claim and execute one task per tick."""
        coordinator = self._get_coordinator()
        if not coordinator:
            return

        # Claim next matching task
        task = coordinator.claim_next_task(
            agent_id=self._node_id,
            capabilities=self._capabilities,
        )
        if not task:
            return

        logger.info(f"Worker claimed task {task.task_id}: {task.description[:80]}")

        # Execute via local /chat
        result = self._execute_task(task)

        if result is not None:
            # Submit result back to coordinator
            try:
                coordinator.submit_result(task.task_id, self._node_id, result)
                logger.info(f"Worker completed task {task.task_id}")
            except Exception as e:
                logger.warning(f"Worker failed to submit result for {task.task_id}: {e}")
        else:
            logger.warning(f"Worker execution failed for task {task.task_id}")

    def _execute_task(self, task) -> Optional[str]:
        """Execute a distributed task via the local /chat endpoint.

        Uses the same guardrail pipeline as local dispatch.
        """
        prompt = task.context.get('prompt', task.description)
        goal_type = task.context.get('goal_type', 'coding')
        user_id = task.context.get('user_id', self._node_id)

        # GUARDRAIL: pre-dispatch gate
        try:
            from security.hive_guardrails import GuardrailEnforcer
            allowed, reason, prompt = GuardrailEnforcer.before_dispatch(prompt)
            if not allowed:
                logger.warning(f"Worker task {task.task_id} blocked by guardrail: {reason}")
                return None
        except ImportError:
            logger.error("CRITICAL: hive_guardrails not available — blocking worker dispatch")
            return None

        base_url = os.environ.get('HEVOLVE_BASE_URL', 'http://localhost:6777')
        prompt_id = f"{goal_type}_{task.task_id[:8]}"

        body = {
            'user_id': user_id,
            'prompt_id': prompt_id,
            'prompt': prompt,
            'create_agent': True,
            'autonomous': True,
            'casual_conv': False,
        }

        try:
            resp = requests.post(f'{base_url}/chat', json=body, timeout=120)
            if resp.status_code == 200:
                result = resp.json()
                response = result.get('response', '')

                # GUARDRAIL: post-response check
                try:
                    from security.hive_guardrails import GuardrailEnforcer
                    passed, reason = GuardrailEnforcer.after_response(response)
                    if not passed:
                        logger.warning(f"Worker response filtered for {task.task_id}: {reason}")
                        return None
                except ImportError:
                    return None

                # Record to world model
                try:
                    from integrations.agent_engine.world_model_bridge import get_world_model_bridge
                    bridge = get_world_model_bridge()
                    bridge.record_interaction(
                        user_id=user_id,
                        prompt_id=prompt_id,
                        prompt=prompt,
                        response=response,
                        goal_id=task.task_id,
                    )
                except Exception:
                    pass

                return response
        except requests.RequestException as e:
            logger.warning(f"Worker local /chat failed for {task.task_id}: {e}")

        return None

    @staticmethod
    def _get_coordinator():
        """Get shared coordinator singleton."""
        try:
            from integrations.distributed_agent.api import _get_coordinator
            return _get_coordinator()
        except Exception:
            return None


# Module-level singleton
worker_loop = DistributedWorkerLoop()
