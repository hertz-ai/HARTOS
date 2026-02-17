"""
Unified Agent Goal Engine - World Model Bridge

Bridge between LLM-langchain orchestration and crawl4ai's embodied AI.
Every agent interaction becomes training data for continuous learning.
Skills distribute via gossip notification + local RALT ingestion.

Dual-mode operation:
  IN-PROCESS (flat/regional): Direct Python calls — zero HTTP overhead.
    crawl4ai is pip-installed, learning functions called directly.
  HTTP FALLBACK (central standalone): REST calls to remote crawl4ai.
    Used when services run as separate processes on different ports.

Bootstrap: crawl4ai uses llama.cpp (Qwen3-VL-2B, Q4_K_XL, ~1.5GB) locally.
No external API key needed.  Learning is local-first, distributed via RALT + WAMP.

GUARDRAILS applied at every layer:
- ConstitutionalFilter on experiences before storage
- WorldModelSafetyBounds on RALT export (rate limit + witness)
- ConstructiveFilter on skill packets (no destructive capabilities)
"""
import json
import logging
import os
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

logger = logging.getLogger('hevolve_social')


class WorldModelBridge:
    """Bridge between LLM-langchain orchestration and crawl4ai embodied AI.

    Dual-mode: in-process direct Python calls when crawl4ai is co-located
    (flat/regional), HTTP fallback when running as separate processes (central).

    Two-tier thinking model:
    - Agent-level: Hevolve dispatches coarse-grained goals (task delegation)
    - Tensor-level: crawl4ai fuses heterogeneous agent thoughts (HiveMind)
    This bridge connects the two layers.
    """

    def __init__(self):
        self._api_url = os.environ.get(
            'CRAWL4AI_API_URL', 'http://localhost:8000')
        self._node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
        self._experience_queue: deque = deque(maxlen=10000)
        self._flush_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='wm_flush')
        self._flush_batch_size = int(os.environ.get(
            'HEVOLVE_WM_FLUSH_BATCH', '50'))
        self._lock = threading.Lock()
        self._stats = {
            'total_recorded': 0,
            'total_flushed': 0,
            'total_corrections': 0,
            'total_hivemind_queries': 0,
            'total_skills_distributed': 0,
            'total_skills_blocked': 0,
            'node_tier': self._node_tier,
        }

        # Configurable HTTP timeouts
        self._timeout_flush = int(os.environ.get('HEVOLVE_WM_FLUSH_TIMEOUT', '15'))
        self._timeout_correction = int(os.environ.get('HEVOLVE_WM_CORRECTION_TIMEOUT', '30'))
        self._timeout_default = int(os.environ.get('HEVOLVE_WM_HTTP_TIMEOUT', '10'))

        # Circuit breaker: after N consecutive failures, stop calling for cooldown period
        self._cb_failures = 0
        self._cb_threshold = 5  # Open circuit after 5 consecutive failures
        self._cb_cooldown = 60  # Seconds to wait before half-open retry
        self._cb_opened_at = 0.0  # Timestamp when circuit opened

        # In-process mode: direct Python calls (no HTTP overhead)
        self._provider = None  # LearningLLMProvider
        self._hive_mind = None  # HiveMind
        self._in_process = False
        self._in_process_retry_done = False
        self._federation_aggregated = {}

        # Cloud consent: per-user gate for non-local HTTP data sharing.
        # "If anyways it needs to be sent to cloud for something then
        #  let's get consent from user at runtime." — project steward
        self._consent_cache: Dict[str, tuple] = {}  # user_id → (bool, timestamp)
        self._consent_cache_ttl = 300  # 5 minutes

        self._init_in_process()

    def _cb_is_open(self) -> bool:
        """Check if circuit breaker is open (blocking requests)."""
        if self._cb_failures < self._cb_threshold:
            return False
        # Circuit is open — check if cooldown has elapsed (half-open)
        if time.time() - self._cb_opened_at > self._cb_cooldown:
            return False  # Allow one retry (half-open state)
        return True

    def _cb_record_success(self):
        """Reset circuit breaker on successful call."""
        self._cb_failures = 0

    def _cb_record_failure(self):
        """Record failure; open circuit at threshold."""
        self._cb_failures += 1
        if self._cb_failures >= self._cb_threshold:
            self._cb_opened_at = time.time()
            logger.warning(
                f"[WorldModelBridge] Circuit breaker OPEN after "
                f"{self._cb_failures} failures. Cooldown {self._cb_cooldown}s.")

    def _is_external_target(self) -> bool:
        """Check if the API URL points to a non-local (cloud) endpoint."""
        url = self._api_url.lower()
        local_prefixes = (
            'http://localhost', 'http://127.0.0.1',
            'http://0.0.0.0', 'http://[::1]',
        )
        return not any(url.startswith(p) for p in local_prefixes)

    def _has_cloud_consent(self, user_id: str) -> bool:
        """Check if a user has consented to cloud data sharing.

        Consent is stored in User.settings['cloud_data_consent'].
        Cached for 5 minutes to avoid DB lookups on every experience.
        In-process mode (local) does NOT require consent — data stays local.
        """
        if not user_id:
            return False

        now = time.time()
        cached = self._consent_cache.get(user_id)
        if cached and now - cached[1] < self._consent_cache_ttl:
            return cached[0]

        consent = False
        try:
            from integrations.social.models import get_db, User
            db = get_db()
            try:
                user = db.query(User).filter_by(id=user_id).first()
                if user:
                    consent = bool(
                        (user.settings or {}).get('cloud_data_consent', False))
            finally:
                db.close()
        except Exception:
            pass

        self._consent_cache[user_id] = (consent, now)
        return consent

    def _init_in_process(self):
        """Try to connect to in-process learning pipeline (zero HTTP overhead).

        When crawl4ai is pip-installed and _init_learning_pipeline() has run
        in langchain_gpt_api.py, we get direct references to the provider
        and hivemind instances. All subsequent calls bypass HTTP entirely.
        """
        try:
            from langchain_gpt_api import get_learning_provider, get_hive_mind
            provider = get_learning_provider()
            hive = get_hive_mind()
            if provider is not None:
                self._provider = provider
                self._hive_mind = hive
                self._in_process = True
                logger.info(
                    "[WorldModelBridge] In-process mode: direct Python calls")
                return
        except ImportError:
            pass
        logger.info(
            f"[WorldModelBridge] HTTP mode: {self._api_url}")

    # ─── Record interactions (auto-learn) ────────────────────────────

    def record_interaction(self, user_id: str, prompt_id: str,
                           prompt: str, response: str,
                           model_id: str = None, latency_ms: float = 0,
                           node_id: str = None, goal_id: str = None):
        """Record every agent interaction as training data for crawl4ai.

        Called after EVERY /chat response.  Batches experiences and flushes
        them to crawl4ai (in-process or HTTP).
        crawl4ai auto-learns from every completion (3-priority queue:
        expert > reality > distillation).

        GUARDRAIL: ConstitutionalFilter screens before storage.
        """
        # Lazy in-process re-check: crawl4ai may have initialized after our __init__
        if not self._in_process and not self._in_process_retry_done:
            self._in_process_retry_done = True
            self._init_in_process()

        try:
            from security.hive_guardrails import ConstitutionalFilter
            passed, _ = ConstitutionalFilter.check_prompt(response)
            if not passed:
                return
        except ImportError:
            pass

        experience = {
            'prompt': prompt[:2000],
            'response': response[:5000],
            'model_id': model_id or 'unknown',
            'latency_ms': latency_ms,
            'user_id': str(user_id),
            'prompt_id': str(prompt_id),
            'node_id': node_id,
            'goal_id': goal_id,
            'timestamp': time.time(),
            'source': 'langchain_orchestration',
        }

        # PRIVACY: Redact secrets + anonymize user before shared ingestion.
        # The hive must NEVER leak secrets from one user to another.
        try:
            from security.secret_redactor import redact_experience
            experience = redact_experience(experience)
        except ImportError:
            pass

        self._experience_queue.append(experience)
        with self._lock:
            self._stats['total_recorded'] += 1

        if len(self._experience_queue) >= self._flush_batch_size:
            batch = []
            while self._experience_queue and len(batch) < self._flush_batch_size:
                try:
                    batch.append(self._experience_queue.popleft())
                except IndexError:
                    break
            if batch:
                self._flush_executor.submit(self._flush_to_world_model, batch)

    def _flush_to_world_model(self, batch: list):
        """Flush experience batch to crawl4ai's learning provider.

        In-process mode: calls provider.create_chat_completion() directly.
        HTTP mode: POST /v1/chat/completions (OpenAI format).
        """
        if self._in_process and self._provider:
            for exp in batch:
                try:
                    messages = [
                        {
                            'role': 'system',
                            'content': json.dumps({
                                'source': exp.get('source',
                                                  'langchain_orchestration'),
                                'user_id': exp.get('user_id'),
                                'prompt_id': exp.get('prompt_id'),
                                'goal_id': exp.get('goal_id'),
                                'model_id': exp.get('model_id'),
                                'latency_ms': exp.get('latency_ms'),
                                'node_id': exp.get('node_id'),
                            }),
                        },
                        {'role': 'user', 'content': exp['prompt']},
                        {'role': 'assistant', 'content': exp['response']},
                    ]
                    self._provider.create_chat_completion(
                        messages=messages,
                        model='hevolve-interaction-replay',
                        temperature=0,
                        max_tokens=1,
                    )
                    with self._lock:
                        self._stats['total_flushed'] += 1
                except Exception as e:
                    logger.debug(f"In-process flush error: {e}")
            return

        # HTTP fallback (central standalone or crawl4ai not in-process)
        if self._cb_is_open():
            logger.debug("[WorldModelBridge] Circuit breaker open — skipping HTTP flush")
            return

        # CONSENT GATE: if target is external (cloud), filter to consented users only.
        # Local endpoints (localhost) don't require consent — data stays on-device.
        if self._is_external_target():
            original_count = len(batch)
            batch = [
                exp for exp in batch
                if self._has_cloud_consent(exp.get('user_id', ''))
            ]
            skipped = original_count - len(batch)
            if skipped > 0:
                logger.info(
                    f"[WorldModelBridge] Skipped {skipped}/{original_count} "
                    f"experiences — no cloud consent")
            if not batch:
                return

        for exp in batch:
            try:
                body = {
                    'model': 'hevolve-interaction-replay',
                    'messages': [
                        {
                            'role': 'system',
                            'content': json.dumps({
                                'source': exp.get('source',
                                                  'langchain_orchestration'),
                                'user_id': exp.get('user_id'),
                                'prompt_id': exp.get('prompt_id'),
                                'goal_id': exp.get('goal_id'),
                                'model_id': exp.get('model_id'),
                                'latency_ms': exp.get('latency_ms'),
                                'node_id': exp.get('node_id'),
                            }),
                        },
                        {'role': 'user', 'content': exp['prompt']},
                        {'role': 'assistant', 'content': exp['response']},
                    ],
                    'temperature': 0,
                    'max_tokens': 1,
                }
                requests.post(
                    f'{self._api_url}/v1/chat/completions',
                    json=body,
                    timeout=self._timeout_flush,
                )
                self._cb_record_success()
                with self._lock:
                    self._stats['total_flushed'] += 1
            except requests.RequestException:
                self._cb_record_failure()
            except Exception as e:
                self._cb_record_failure()
                logger.debug(f"World model flush error: {e}")

    # ─── Expert corrections (RL-EF) ─────────────────────────────────

    def submit_correction(self, original_response: str,
                          corrected_response: str,
                          expert_id: str = 'hevolve_user',
                          confidence: float = 1.0,
                          explanation: str = None,
                          context: dict = None,
                          valid_until: str = None) -> dict:
        """Submit an expert correction to crawl4ai's RL-EF system.

        In-process: calls send_expert_correction() directly.
        HTTP: POST /v1/corrections.

        crawl4ai routes to:
        - Kernel Continual Learner (instant, no gradient) for factual corrections
        - Orthogonal LoRA (gradient-based, forgetting-safe) for conceptual ones

        Returns dict with success status.
        """
        try:
            from security.hive_guardrails import ConstitutionalFilter
            passed, reason = ConstitutionalFilter.check_prompt(corrected_response)
            if not passed:
                return {'success': False, 'reason': reason}
        except ImportError:
            pass

        # PRIVACY: Redact secrets from corrections before shared learning
        try:
            from security.secret_redactor import redact_secrets
            original_response, _ = redact_secrets(original_response)
            corrected_response, _ = redact_secrets(corrected_response)
            if explanation:
                explanation, _ = redact_secrets(explanation)
        except ImportError:
            pass

        # In-process direct call
        if self._in_process and self._provider:
            try:
                from crawl4ai.embodied_ai.rl_ef import send_expert_correction
                result = send_expert_correction(
                    domain='general',
                    original_response=original_response[:5000],
                    corrected_response=corrected_response[:5000],
                    expert_id=expert_id,
                    confidence=max(0.0, min(1.0, confidence)),
                    explanation=explanation[:2000] if explanation else None,
                    valid_until=valid_until,
                )
                with self._lock:
                    self._stats['total_corrections'] += 1
                return result if isinstance(result, dict) else {'success': True}
            except Exception as e:
                logger.debug(f"In-process correction failed: {e}")

        # CONSENT GATE: external HTTP requires consent
        if self._is_external_target():
            user_id = (context or {}).get('user_id', expert_id)
            if not self._has_cloud_consent(user_id):
                return {'success': False, 'reason': 'Cloud data consent required'}

        # HTTP fallback
        body = {
            'original_response': original_response[:5000],
            'corrected_response': corrected_response[:5000],
            'expert_id': expert_id,
            'confidence': max(0.0, min(1.0, confidence)),
        }
        if explanation:
            body['explanation'] = explanation[:2000]
        if context:
            body['context'] = context
        if valid_until:
            body['valid_until'] = valid_until

        if self._cb_is_open():
            return {'success': False, 'reason': 'Circuit breaker open'}

        try:
            resp = requests.post(
                f'{self._api_url}/v1/corrections',
                json=body,
                timeout=self._timeout_correction,
            )
            self._cb_record_success()
            if resp.status_code == 200:
                with self._lock:
                    self._stats['total_corrections'] += 1
                return resp.json()
            return {'success': False, 'reason': f'HTTP {resp.status_code}'}
        except requests.RequestException as e:
            self._cb_record_failure()
            logger.debug(f"Correction submission failed: {e}")
            return {'success': False, 'reason': str(e)}

    # ─── HiveMind collective thinking ────────────────────────────────

    def query_hivemind(self, query_text: str,
                       timeout_ms: int = 1000,
                       user_id: str = None) -> Optional[dict]:
        """Query crawl4ai's HiveMind for distributed collective thinking.

        In-process: calls hive_mind.think_together_distributed() directly.
        HTTP: POST /v1/hivemind/think.

        crawl4ai:
        1. Encodes query via frozen Qwen alignment layer (2048-D)
        2. Publishes local thought to WAMP
        3. Waits for remote agent responses (timeout_ms)
        4. Fuses with attention-weighted method
        5. Returns collective thought with contributing agents + weights

        Use this for real-time multi-modal reasoning (tensor-level fusion).
        For coarse-grained task delegation, use agent-level dispatch instead.
        """
        try:
            from security.hive_guardrails import ConstitutionalFilter
            passed, _ = ConstitutionalFilter.check_prompt(query_text)
            if not passed:
                return None
        except ImportError:
            pass

        # In-process direct call
        if self._in_process and self._hive_mind:
            try:
                import torch
                # Encode query text as a thought tensor
                thought = torch.randn(1, 2048)  # Placeholder encoding
                # Use provider's encoder if available
                if (self._provider and
                        hasattr(self._provider, 'embodied_agent') and
                        self._provider.embodied_agent and
                        hasattr(self._provider.embodied_agent, 'encoder')):
                    encoder = self._provider.embodied_agent.encoder
                    thought = encoder.encode_text(query_text)

                result = self._hive_mind.think_together_distributed(
                    local_thought=thought,
                    local_agent_id=getattr(
                        self._hive_mind, '_local_agent_id', 'local'),
                    timeout_ms=timeout_ms,
                )
                with self._lock:
                    self._stats['total_hivemind_queries'] += 1
                return {
                    'thought': result.text if hasattr(result, 'text')
                    else str(result)
                }
            except Exception as e:
                logger.debug(f"In-process hivemind failed: {e}")

        # PRIVACY: Redact secrets from query before sending to shared hivemind
        try:
            from security.secret_redactor import redact_secrets
            query_text, _ = redact_secrets(query_text)
        except ImportError:
            pass

        # CONSENT GATE: external HTTP requires consent
        if self._is_external_target() and user_id:
            if not self._has_cloud_consent(user_id):
                return None

        # HTTP fallback
        if self._cb_is_open():
            return None

        try:
            resp = requests.post(
                f'{self._api_url}/v1/hivemind/think',
                json={'query': query_text[:2000], 'timeout_ms': timeout_ms},
                timeout=max(30, timeout_ms / 1000 + 5),
            )
            self._cb_record_success()
            if resp.status_code == 200:
                with self._lock:
                    self._stats['total_hivemind_queries'] += 1
                return resp.json()
        except requests.RequestException as e:
            self._cb_record_failure()
            logger.debug(f"HiveMind query failed: {e}")
        return None

    # ─── Learning stats ──────────────────────────────────────────────

    def get_learning_stats(self) -> dict:
        """Get merged learning + hivemind + bridge stats.

        In-process: calls provider.get_stats() + hive_mind.get_stats().
        HTTP: GET /v1/stats + GET /v1/hivemind/stats.
        Returns combined dict for dashboard consumption.
        """
        result = {'learning': {}, 'hivemind': {}, 'bridge': self.get_stats()}

        if self._in_process:
            if self._provider:
                try:
                    result['learning'] = self._provider.get_stats()
                except Exception:
                    pass
            if self._hive_mind:
                try:
                    result['hivemind'] = self._hive_mind.get_stats()
                except Exception:
                    pass
            return result

        # HTTP fallback
        try:
            resp = requests.get(
                f'{self._api_url}/v1/stats', timeout=self._timeout_default)
            if resp.status_code == 200:
                result['learning'] = resp.json()
        except requests.RequestException:
            pass

        try:
            resp = requests.get(
                f'{self._api_url}/v1/hivemind/stats', timeout=self._timeout_default)
            if resp.status_code == 200:
                result['hivemind'] = resp.json()
        except requests.RequestException:
            pass

        return result

    def get_hivemind_agents(self) -> list:
        """Get list of connected HiveMind agents.

        In-process: calls hive_mind.get_all_agents().
        HTTP: GET /v1/hivemind/agents.
        Returns agent specs with capabilities, modality, latent dimensions.
        """
        if self._in_process and self._hive_mind:
            try:
                return self._hive_mind.get_all_agents()
            except Exception:
                pass

        # HTTP fallback
        try:
            resp = requests.get(
                f'{self._api_url}/v1/hivemind/agents', timeout=self._timeout_default)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('agents', data) if isinstance(data, dict) else data
        except requests.RequestException:
            pass
        return []

    # ─── RALT skill distribution ─────────────────────────────────────

    def distribute_skill_packet(self, ralt_packet: dict,
                                 node_id: str,
                                 target_nodes: list = None) -> dict:
        """Distribute a learned skill (RALT packet) across hive nodes.

        Gossip broadcasts a notification that a skill is available.
        Each receiving peer triggers RALT ingestion through their own
        local crawl4ai instance.

        GUARDRAILS:
        1. WorldModelSafetyBounds.gate_ralt_export() — rate limit + witness
        2. ConstructiveFilter.check_output() — constructiveness check
        """
        try:
            from security.hive_guardrails import WorldModelSafetyBounds
            passed, reason = WorldModelSafetyBounds.gate_ralt_export(
                ralt_packet, node_id)
            if not passed:
                with self._lock:
                    self._stats['total_skills_blocked'] += 1
                return {'success': False, 'reason': reason}
        except ImportError:
            pass

        try:
            from security.hive_guardrails import ConstructiveFilter
            desc = ralt_packet.get('description', '')
            passed, reason = ConstructiveFilter.check_output(desc)
            if not passed:
                with self._lock:
                    self._stats['total_skills_blocked'] += 1
                return {'success': False, 'reason': reason}
        except ImportError:
            pass

        # Notify peers via gossip that a skill is available
        # Each peer ingests via their local crawl4ai RALT receiver
        try:
            from integrations.social.peer_discovery import gossip
            gossip.broadcast({
                'type': 'ralt_skill_available',
                'packet_summary': {
                    'task_id': ralt_packet.get('task_id'),
                    'description': ralt_packet.get('description', '')[:200],
                    'complexity': ralt_packet.get('complexity', 'unknown'),
                },
                'source_node': node_id,
                'source_api_url': self._api_url,
                'timestamp': time.time(),
            }, targets=target_nodes)
            with self._lock:
                self._stats['total_skills_distributed'] += 1
            return {'success': True}
        except Exception as e:
            logger.debug(f"RALT distribution skipped: {e}")
            return {'success': False, 'reason': str(e)}

    # ─── Health ──────────────────────────────────────────────────────

    def check_health(self) -> dict:
        """Check learning pipeline health.

        In-process: returns healthy if provider is available.
        HTTP: GET /health on crawl4ai API.
        """
        if self._in_process and self._provider:
            return {
                'healthy': True,
                'learning_active': True,
                'mode': 'in_process',
                'node_tier': self._node_tier,
            }

        # HTTP fallback
        try:
            resp = requests.get(
                f'{self._api_url}/health', timeout=5)
            if resp.status_code == 200:
                data = resp.json() if resp.headers.get(
                    'content-type', '').startswith('application/json') else {}
                return {
                    'healthy': True,
                    'learning_active': True,
                    'mode': 'http',
                    'node_tier': self._node_tier,
                    'details': data,
                }
            return {
                'healthy': False,
                'learning_active': False,
                'mode': 'http',
                'node_tier': self._node_tier,
                'details': {'status_code': resp.status_code},
            }
        except requests.RequestException as e:
            return {
                'healthy': False,
                'learning_active': False,
                'mode': 'http',
                'node_tier': self._node_tier,
                'details': {'error': str(e)},
            }

    def get_stats(self) -> dict:
        """Get bridge-level statistics."""
        with self._lock:
            return {
                'queue_size': len(self._experience_queue),
                'api_url': self._api_url,
                'in_process': self._in_process,
                **self._stats,
            }

    # ─── Federation support ───────────────────────────────────────

    def extract_learning_delta(self) -> dict:
        """Pull stats for federation delta extraction.

        Used by FederatedAggregator.extract_local_delta() to build the
        lightweight metric delta that gets broadcast to peers.
        """
        stats = self.get_stats()
        learning = self.get_learning_stats()
        return {
            'bridge': stats,
            'learning': learning.get('learning', {}),
            'hivemind': learning.get('hivemind', {}),
        }

    def apply_federation_update(self, aggregated: dict) -> bool:
        """Store aggregated network-wide metrics locally.

        Does NOT push to crawl4ai — federation metrics are consumed
        by BenchmarkRegistry and dashboard, not crawl4ai's learning pipeline.
        """
        self._federation_aggregated = aggregated
        return True


# ─── Module-level singleton ───
_bridge = None
_bridge_lock = threading.Lock()


def get_world_model_bridge() -> WorldModelBridge:
    """Get or create the singleton WorldModelBridge."""
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = WorldModelBridge()
    return _bridge
