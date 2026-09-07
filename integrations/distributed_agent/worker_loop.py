"""
Distributed Worker Loop — auto-claim and execute tasks on worker nodes.

Runs as a background daemon thread on every node where the shared Redis
coordinator is reachable. No separate mode flag — if Redis exists and
this node is part of a hive, the worker loop auto-starts and claims tasks.

Polls the shared DistributedTaskCoordinator for unclaimed tasks,
executes them via the local /chat endpoint, and submits results back.
"""
import os
import random
import time
import logging
import threading
import requests
from typing import Optional
from core.http_pool import pooled_post

from core.constants import HIVE_DEPTH, HIVE_WORKER_BASE_CAPABILITIES
from core.port_registry import get_port

logger = logging.getLogger('hevolve_social')


class DistributedWorkerLoop:
    """Background loop: claim tasks from shared Redis, execute via local /chat, submit results."""

    # Exponential backoff bounds for Redis outages.  Without this the
    # tick loop used to spin at self._interval (15s) while every Redis
    # call raised ConnectionError — not quite 100% CPU, but tens of
    # thousands of failed connect attempts per hour during a prolonged
    # outage.  With backoff we start at 1s (much quicker than 15s
    # recovery) and cap at 60s (well below the poll interval during
    # healthy operation).
    _BACKOFF_MIN: float = 1.0
    _BACKOFF_MAX: float = 60.0
    # Jitter factor so N workers don't all reconnect in the same second
    # when Redis comes back.  ±25% of the current backoff window.
    _BACKOFF_JITTER: float = 0.25

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_WORKER_POLL_INTERVAL', '15'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._node_id = os.environ.get('HEVOLVE_NODE_ID', 'unknown')
        self._capabilities = self._detect_capabilities()
        # Current Redis backoff state — reset to 0 when a tick succeeds.
        self._redis_backoff: float = 0.0

    def _detect_capabilities(self):
        """Detect this node's capabilities from system_requirements."""
        caps = list(HIVE_WORKER_BASE_CAPABILITIES)  # Base capabilities
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

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking operations."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('distributed_worker')
        except Exception:
            pass

    def _loop(self):
        # Lazy-import redis so tests that never touch Redis don't need
        # the dependency installed.  ``RedisConnectionError`` resolves to
        # ``Exception`` when redis isn't available, which is still
        # correct (the backoff branch also traps generic connect errors).
        try:
            from redis.exceptions import ConnectionError as RedisConnectionError
        except Exception:  # pragma: no cover — redis always installed in prod
            RedisConnectionError = ConnectionError  # type: ignore[misc, assignment]

        while self._running:
            # Sleep for poll interval + current backoff.  Backoff is 0
            # during healthy operation so this collapses back to the
            # pre-fix behaviour once Redis recovers.
            sleep_for = self._interval + self._redis_backoff
            if self._redis_backoff > 0:
                # ±25% jitter prevents thundering-herd when many workers
                # simultaneously discover Redis has come back.
                jitter = self._redis_backoff * self._BACKOFF_JITTER
                sleep_for += random.uniform(-jitter, jitter)
            time.sleep(max(1.0, sleep_for))
            if not self._running:
                break
            self._wd_heartbeat()
            # GUARDRAIL: circuit breaker
            try:
                from security.hive_guardrails import HiveCircuitBreaker
                if HiveCircuitBreaker.is_halted():
                    continue
            except ImportError:
                pass
            try:
                self._tick()
                # Tick succeeded → reset backoff so the next cycle runs
                # at the normal poll cadence.
                if self._redis_backoff > 0:
                    logger.info(
                        f"Worker Redis recovered after "
                        f"{self._redis_backoff:.1f}s backoff")
                    self._redis_backoff = 0.0
            except RedisConnectionError as e:
                self._bump_redis_backoff()
                logger.warning(
                    f"Worker Redis ConnectionError: {e}; "
                    f"backoff={self._redis_backoff:.1f}s")
            except Exception as e:
                # Unknown errors: treat as transient but still back off so
                # a persistent bug doesn't spin.
                msg = str(e).lower()
                if 'connection' in msg or 'timeout' in msg or 'redis' in msg:
                    self._bump_redis_backoff()
                logger.debug(f"Distributed worker tick error: {e}")
            self._wd_heartbeat()

    def _bump_redis_backoff(self) -> None:
        """Double the backoff window, clamped to [MIN, MAX]."""
        if self._redis_backoff <= 0:
            self._redis_backoff = self._BACKOFF_MIN
        else:
            self._redis_backoff = min(self._redis_backoff * 2,
                                      self._BACKOFF_MAX)

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

        # HIVE_DEPTH enforcement — defense in depth.  Coordinators stamp
        # hop at submit_goal, but a rogue peer could have forwarded a
        # task with an inflated hop; drop anything past the published
        # 3-level topology instead of executing it and re-propagating.
        try:
            task_hop = int(task.context.get('hop', 0) or 0)
        except (TypeError, ValueError):
            task_hop = 0
        if task_hop >= HIVE_DEPTH:
            logger.warning(
                f"Worker dropping task {task.task_id}: hop={task_hop} "
                f">= HIVE_DEPTH={HIVE_DEPTH}")
            return

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

        base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
        prompt_id = f"{goal_type}_{task.task_id[:8]}"

        body = {
            'user_id': user_id,
            'prompt_id': prompt_id,
            'prompt': prompt,
            'create_agent': True,
            'autonomous': True,
            'casual_conv': False,
        }

        # Canonical in-process route first — the SAME call dispatch_goal
        # makes.  On a bundled desktop the raw POST below does not reach
        # HARTOS at all: Nunba registers its own /chat there, it reads the
        # body's `text` field and we send `prompt`, so the answer is
        # 400 "Text is required".  local_chat_dispatch goes through
        # routes.hartos_backend_adapter.chat, which is the declared
        # translator between the two dialects, and it also applies the
        # user-priority gate and the local-LLM semaphore that this loop
        # skipped entirely.
        from integrations.agent_engine.dispatch import (
            _internal_auth_headers, local_chat_dispatch,
        )

        _status, _text = local_chat_dispatch(
            prompt, user_id, prompt_id, daemon_id=task.task_id,
            native_fallback=False)
        if _status == 'ok' and _text:
            return self._after_response(
                _text, task, user_id=user_id, prompt_id=prompt_id,
                prompt=prompt)
        if _status == 'deferred':
            # A human has the LLM. Returning None here re-queues the task for
            # a later tick, which is the correct outcome — the alternative is
            # taking the LLM away from the person using the machine.
            logger.info(f"Worker deferring task {task.task_id}: local LLM "
                        f"yielded to user activity")
            return None

        # The self-POST carries the credential dispatch.py mints. On the
        # central/regional tiers security/middleware.py gate 2 answers a
        # bare POST /chat with 401 "Authentication required (Bearer token)":
        # measured on central 2026-09-02, every claimed task died here and
        # the ledger re-dispatched it each tick ("Task <id>_root already
        # exists"). Same helper as dispatch.py:862 and
        # speculative_dispatcher.py:1880 (cdd379ad); this was the third site.
        try:
            resp = pooled_post(f'{base_url}/chat', json=body,
                               headers=_internal_auth_headers(), timeout=120)
            if resp.status_code == 200:
                result = resp.json()
                return self._after_response(
                    result.get('response', ''), task,
                    user_id=user_id, prompt_id=prompt_id, prompt=prompt)
            # WARNING, not DEBUG: central emits no INFO/DEBUG after boot, and
            # this branch was a bare None, so the 401 above left no trace
            # beyond "Worker execution failed".
            logger.warning(
                f"Worker local /chat returned HTTP {resp.status_code} for "
                f"{task.task_id}")
        except requests.RequestException as e:
            logger.warning(f"Worker local /chat failed for {task.task_id}: {e}")

        return None

    def _after_response(self, response, task, user_id=None, prompt_id=None,
                        prompt=None):
        """Guardrail + world-model recording for a worker reply.

        Shared by both routes into /chat (the canonical in-process call and
        the HTTP fallback) so a response can never reach the coordinator
        having skipped the post-response guardrail on one path only.
        """
        if not response:
            return None

        # GUARDRAIL: post-response check (fail-closed)
        try:
            from security.hive_guardrails import GuardrailEnforcer
            passed, reason = GuardrailEnforcer.after_response(response)
            if not passed:
                logger.warning(
                    f"Worker response filtered for {task.task_id}: {reason}")
                return None
        except ImportError:
            logger.error("CRITICAL: hive_guardrails not available — "
                         "blocking worker response")
            return None

        # Record to world model
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
            bridge.record_interaction(
                user_id=user_id or self._node_id,
                prompt_id=prompt_id or task.task_id,
                prompt=prompt or task.description,
                response=response,
                goal_id=task.task_id,
            )
        except Exception:
            pass

        return response

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
