"""
Central Orchestrator Client — the deployment-side glue between a
running HART OS / Nunba node and hevolve.ai's central control plane.

Brief reference: ml_intern_brief_hevolveai_training.md §2.3 + §5-D.

Responsibilities:
  1. POST /heartbeat  — every node reports health + benchmark best-scores
     to the central orchestrator so the ops dashboard shows live status.
  2. GET  /halt       — the node polls for the master kill signal.  When
     present, the returned payload carries a master-key signature; we
     verify via security.master_key.verify_master_signature before
     calling HiveCircuitBreaker.halt_network().
  3. (optional) emit TensorBoard local-writer path to a central ingest
     URL — consent-gated, controlled by a single env var.

Design constraints:
  - NO URL is invented.  Every endpoint is driven by environment
    variables.  If the env var is unset, the client does nothing
    (fails open — the node operates independently).  This matches
    the brief's §2.3 note: "Do not invent these URLs. Ask the
    human-in-the-loop."
  - Poll loop runs in a single background daemon thread with
    exponential backoff on failure.  Never spams a dead central.
  - When the node tier is 'central' itself, the client SHORTS OUT —
    a central node does not POST heartbeats to itself.
  - Master-kill signature verification is MANDATORY.  A halt signal
    without a verified master-key signature is logged at CRITICAL
    and ignored.  This prevents a rogue central from halting the
    network without the physical master key.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_social')


# ─── Environment variables — all URLs driven by these ───

ENV_CENTRAL_URL = 'HEVOLVE_CENTRAL_ORCHESTRATOR_URL'
ENV_HEARTBEAT_PATH = 'HEVOLVE_CENTRAL_HEARTBEAT_PATH'     # default /heartbeat
ENV_HALT_PATH = 'HEVOLVE_CENTRAL_HALT_PATH'               # default /halt
ENV_TENSORBOARD_URL = 'HEVOLVE_TENSORBOARD_URL'
ENV_HEARTBEAT_INTERVAL = 'HEVOLVE_CENTRAL_HEARTBEAT_INTERVAL_S'   # default 60
ENV_HALT_POLL_INTERVAL = 'HEVOLVE_CENTRAL_HALT_POLL_INTERVAL_S'   # default 30
ENV_NODE_ID = 'HEVOLVE_NODE_ID'
ENV_NODE_TIER = 'HEVOLVE_NODE_TIER'


_DEFAULT_HEARTBEAT_PATH = '/heartbeat'
_DEFAULT_HALT_PATH = '/halt'
_DEFAULT_HEARTBEAT_INTERVAL = 60
_DEFAULT_HALT_POLL_INTERVAL = 30
_MAX_BACKOFF = 600       # 10 minutes
_INITIAL_BACKOFF = 5     # 5 seconds


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default


class CentralOrchestratorClient:
    """Background client polling the hevolve.ai central orchestrator.

    Call `start()` from boot — idempotent and a no-op when the central
    URL env var is unset.  `stop()` is called at process shutdown.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._last_heartbeat_ts: float = 0.0
        self._last_heartbeat_error: Optional[str] = None
        self._last_halt_poll_ts: float = 0.0
        self._last_halt_poll_error: Optional[str] = None
        self._halt_applied = False
        self._backoff = _INITIAL_BACKOFF

    # ─── Public API ───

    def is_configured(self) -> bool:
        """True when the central URL env var is set (non-empty)."""
        url = os.environ.get(ENV_CENTRAL_URL, '').strip()
        return bool(url)

    def start(self) -> bool:
        """Start the poll loop.  No-op when env not configured OR the
        node tier is 'central' (central node doesn't phone itself).
        Returns True when the thread actually started."""
        if not self.is_configured():
            logger.info(
                '[central_orchestrator] %s unset — client inactive',
                ENV_CENTRAL_URL,
            )
            return False
        tier = (os.environ.get(ENV_NODE_TIER, '') or '').lower()
        if tier == 'central':
            logger.info(
                '[central_orchestrator] node tier=central — client '
                'does not self-heartbeat'
            )
            return False
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name='central_orchestrator_client',
                daemon=True,
            )
            self._running = True
            self._thread.start()
        logger.info(
            '[central_orchestrator] started — target=%s heartbeat_interval=%ds',
            os.environ.get(ENV_CENTRAL_URL, ''),
            _int_env(ENV_HEARTBEAT_INTERVAL, _DEFAULT_HEARTBEAT_INTERVAL),
        )
        return True

    def stop(self) -> None:
        """Signal the loop to exit.  Does NOT join — daemon thread
        exits when the process exits."""
        self._stop_event.set()
        with self._lock:
            self._running = False

    def get_status(self) -> Dict[str, Any]:
        """Expose current state for /status endpoints + dashboards."""
        return {
            'configured': self.is_configured(),
            'running': self._running,
            'last_heartbeat_ts': self._last_heartbeat_ts,
            'last_heartbeat_error': self._last_heartbeat_error,
            'last_halt_poll_ts': self._last_halt_poll_ts,
            'last_halt_poll_error': self._last_halt_poll_error,
            'halt_applied': self._halt_applied,
            'central_url': os.environ.get(ENV_CENTRAL_URL, ''),
            'tensorboard_url': os.environ.get(ENV_TENSORBOARD_URL, ''),
        }

    # ─── Loop body ───

    def _loop(self) -> None:
        heartbeat_interval = _int_env(
            ENV_HEARTBEAT_INTERVAL, _DEFAULT_HEARTBEAT_INTERVAL,
        )
        halt_interval = _int_env(
            ENV_HALT_POLL_INTERVAL, _DEFAULT_HALT_POLL_INTERVAL,
        )

        next_heartbeat = 0.0
        next_halt_poll = 0.0

        while not self._stop_event.is_set():
            now = time.time()
            try:
                if now >= next_heartbeat:
                    ok = self._post_heartbeat()
                    if ok:
                        self._backoff = _INITIAL_BACKOFF
                        next_heartbeat = now + heartbeat_interval
                    else:
                        # Backoff — jittered, capped at _MAX_BACKOFF
                        delay = min(self._backoff, _MAX_BACKOFF)
                        delay = delay + random.uniform(0, delay * 0.2)
                        next_heartbeat = now + delay
                        self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

                if now >= next_halt_poll:
                    self._check_halt()
                    next_halt_poll = now + halt_interval
            except Exception as exc:
                # Never let an exception kill the loop — the whole point
                # of this thread is to survive central outages.
                logger.debug(f'[central_orchestrator] loop error: {exc}')

            # Sleep until the sooner of the two next deadlines, but at
            # most 5 seconds so `stop()` takes effect promptly.
            sleep_for = max(
                0.5,
                min(5.0, min(next_heartbeat, next_halt_poll) - time.time()),
            )
            self._stop_event.wait(sleep_for)

    # ─── Heartbeat ───

    def _post_heartbeat(self) -> bool:
        url = self._url(ENV_HEARTBEAT_PATH, _DEFAULT_HEARTBEAT_PATH)
        if not url:
            return False
        payload = self._build_heartbeat_payload()
        try:
            from core.http_pool import pooled_post
            resp = pooled_post(url, json=payload, timeout=10)
            self._last_heartbeat_ts = time.time()
            if resp is None:
                self._last_heartbeat_error = 'no response'
                return False
            status = getattr(resp, 'status_code', 0)
            if 200 <= status < 300:
                self._last_heartbeat_error = None
                return True
            self._last_heartbeat_error = f'HTTP {status}'
            return False
        except ImportError:
            return self._post_heartbeat_requests(url, payload)
        except Exception as exc:
            self._last_heartbeat_ts = time.time()
            self._last_heartbeat_error = str(exc)
            return False

    def _post_heartbeat_requests(
        self, url: str, payload: Dict[str, Any],
    ) -> bool:
        """Fallback for environments where core.http_pool is absent
        (e.g. slim import-only test runs).  Uses `requests` with a
        short timeout."""
        try:
            import requests
            resp = requests.post(url, json=payload, timeout=10)
            self._last_heartbeat_ts = time.time()
            if 200 <= resp.status_code < 300:
                self._last_heartbeat_error = None
                return True
            self._last_heartbeat_error = f'HTTP {resp.status_code}'
            return False
        except Exception as exc:
            self._last_heartbeat_ts = time.time()
            self._last_heartbeat_error = str(exc)
            return False

    def _build_heartbeat_payload(self) -> Dict[str, Any]:
        """Assemble the heartbeat body.

        Fields are intentionally conservative — no raw user data, no
        PII.  The central orchestrator sees node_id, node_tier,
        guardrail_hash, and a small benchmark summary.  Everything
        else stays on-device.
        """
        payload: Dict[str, Any] = {
            'node_id': os.environ.get(ENV_NODE_ID, '') or _fallback_node_id(),
            'node_tier': os.environ.get(ENV_NODE_TIER, '') or 'flat',
            'timestamp': time.time(),
            'version': 1,
        }
        # Guardrail hash — proves we're still running genuine guardrails.
        try:
            from security.hive_guardrails import compute_guardrail_hash
            payload['guardrail_hash'] = compute_guardrail_hash()
        except Exception:
            pass
        # Halted-state flag — the central wants to know when a node
        # has self-tripped its circuit breaker.
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            payload['halted'] = HiveCircuitBreaker.is_halted()
        except Exception:
            payload['halted'] = False
        # Benchmark best scores — lets the central dashboard rank nodes.
        try:
            from integrations.agent_engine.hive_benchmark_prover import (
                get_benchmark_prover,
            )
            prover = get_benchmark_prover()
            best = prover._leaderboard.get_best_scores() or {}
            # Only top-level benchmark scores — keep the payload small.
            payload['benchmark_best'] = {
                k: round(float(v.get('score', 0)), 4)
                for k, v in best.items()
            }
        except Exception:
            payload['benchmark_best'] = {}
        # WorldModelBridge stats — one-line summary of learning traffic.
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            b = get_world_model_bridge()
            stats = b.get_stats() if hasattr(b, 'get_stats') else {}
            payload['world_model'] = {
                'total_recorded': stats.get('total_recorded', 0),
                'total_flushed': stats.get('total_flushed', 0),
            }
        except Exception:
            payload['world_model'] = {}
        return payload

    # ─── Halt poll ───

    def _check_halt(self) -> None:
        url = self._url(ENV_HALT_PATH, _DEFAULT_HALT_PATH)
        if not url:
            return
        try:
            from core.http_pool import pooled_get
            resp = pooled_get(url, timeout=10)
        except ImportError:
            resp = self._get_halt_requests(url)
        except Exception as exc:
            self._last_halt_poll_ts = time.time()
            self._last_halt_poll_error = str(exc)
            return

        self._last_halt_poll_ts = time.time()
        if resp is None:
            self._last_halt_poll_error = 'no response'
            return
        status = getattr(resp, 'status_code', 0)
        if status == 404:
            # Central orchestrator defines "no halt" as 404 on /halt —
            # cheap, no JSON parse needed.
            self._last_halt_poll_error = None
            return
        if status != 200:
            self._last_halt_poll_error = f'HTTP {status}'
            return
        try:
            body = resp.json() if hasattr(resp, 'json') else None
        except Exception:
            self._last_halt_poll_error = 'invalid JSON'
            return
        if not isinstance(body, dict) or not body.get('halt'):
            self._last_halt_poll_error = None
            return
        reason = str(body.get('reason', 'central halt'))
        signature = str(body.get('signature', ''))
        if not signature:
            logger.critical(
                '[central_orchestrator] halt signal WITHOUT signature '
                '— ignored.  Reason: %s', reason,
            )
            self._last_halt_poll_error = 'halt without signature'
            return
        self._apply_halt(reason=reason, signature=signature)
        self._last_halt_poll_error = None

    def _get_halt_requests(self, url: str):
        try:
            import requests
            return requests.get(url, timeout=10)
        except Exception as exc:
            self._last_halt_poll_error = str(exc)
            return None

    def _apply_halt(self, reason: str, signature: str) -> None:
        """Verify the master-key signature + trip the circuit breaker.

        HiveCircuitBreaker.halt_network itself verifies the signature,
        so a forged halt signal is rejected at the guardrail layer.
        This method is just the caller.
        """
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            ok = HiveCircuitBreaker.halt_network(
                reason=f'central:{reason}',
                signature=signature,
            )
            if ok:
                self._halt_applied = True
                logger.critical(
                    '[central_orchestrator] HIVE HALTED by central '
                    'orchestrator.  Reason: %s', reason,
                )
            else:
                logger.critical(
                    '[central_orchestrator] halt signal rejected — '
                    'signature verification failed.  Reason: %s',
                    reason,
                )
        except ImportError:
            logger.critical(
                '[central_orchestrator] guardrails unavailable — '
                'cannot apply halt: %s', reason,
            )
        except Exception as exc:
            logger.critical(
                '[central_orchestrator] halt apply failed: %s', exc,
            )

    # ─── URL helpers ───

    def _url(self, path_env: str, default_path: str) -> str:
        base = os.environ.get(ENV_CENTRAL_URL, '').strip()
        if not base:
            return ''
        if base.endswith('/'):
            base = base[:-1]
        path = os.environ.get(path_env, '').strip() or default_path
        if not path.startswith('/'):
            path = '/' + path
        return base + path


def _fallback_node_id() -> str:
    """Return a stable-per-install node id when HEVOLVE_NODE_ID is unset."""
    try:
        from security.node_integrity import compute_code_hash
        return compute_code_hash()[:16]
    except Exception:
        return 'unknown-node'


# ─── Module singleton ───

_client: Optional[CentralOrchestratorClient] = None
_client_lock = threading.Lock()


def get_client() -> CentralOrchestratorClient:
    """Return the singleton client, creating it on first access."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = CentralOrchestratorClient()
    return _client


def start() -> bool:
    """Idempotent bootstrap helper — call from hart_intelligence_entry
    boot.  Returns True when the background loop actually started."""
    return get_client().start()


def stop() -> None:
    """Idempotent shutdown helper — call from atexit."""
    get_client().stop()


def get_status() -> Dict[str, Any]:
    """Status snapshot for /api/social/dashboard/health."""
    return get_client().get_status()
