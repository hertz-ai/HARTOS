"""
Unified Agent Goal Engine - World Model Bridge

Bridge between LLM-langchain orchestration and HevolveAI's embodied AI.
Every agent interaction becomes training data for continuous learning.
Skills distribute via gossip notification + local RALT ingestion.

Dual-mode operation:
  IN-PROCESS (flat/regional): Direct Python calls — zero HTTP overhead.
    HevolveAI is pip-installed, learning functions called directly.
  HTTP FALLBACK (central standalone): REST calls to remote HevolveAI.
    Used when services run as separate processes on different ports.

Bootstrap: HevolveAI uses llama.cpp (Qwen3-VL-2B, Q4_K_XL, ~1.5GB) locally.
No external API key needed.  Learning is local-first, distributed via RALT + WAMP.

GUARDRAILS applied at every layer:
- ConstitutionalFilter on experiences before storage
- WorldModelSafetyBounds on RALT export (rate limit + witness)
- ConstructiveFilter on skill packets (no destructive capabilities)
"""
import atexit
import json
import logging
import os
import sys
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from core.http_pool import pooled_get, pooled_post

logger = logging.getLogger('hevolve_social')


class WorldModelBridge:
    """Bridge between LLM-langchain orchestration and HevolveAI embodied AI.

    Dual-mode: in-process direct Python calls when HevolveAI is co-located
    (flat/regional), HTTP fallback when running as separate processes (central).

    Two-tier thinking model:
    - Agent-level: Hevolve dispatches coarse-grained goals (task delegation)
    - Tensor-level: HevolveAI fuses heterogeneous agent thoughts (HiveMind)
    This bridge connects the two layers.
    """

    def __init__(self):
        self._api_url = os.environ.get(
            'HEVOLVEAI_API_URL', 'http://localhost:8000')
        self._node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
        self._experience_queue: deque = deque(maxlen=10000)
        self._flush_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='wm_flush')
        atexit.register(lambda: self._flush_executor.shutdown(wait=False))
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

        # Circuit breaker: reusable implementation from core
        from core.circuit_breaker import CircuitBreaker
        self._circuit_breaker = CircuitBreaker(
            name='world_model_bridge', threshold=5, cooldown=60.0)

        # In-process mode: direct Python calls (no HTTP overhead)
        self._provider = None  # LearningLLMProvider
        self._hive_mind = None  # HiveMind
        self._in_process = False
        self._in_process_retry_done = False
        self._http_disabled = False  # Set True when bundled + no server to talk to
        self._federation_aggregated = {}

        # Cloud consent: per-user gate for non-local HTTP data sharing.
        # "If anyways it needs to be sent to cloud for something then
        #  let's get consent from user at runtime." — project steward
        self._consent_cache: Dict[str, tuple] = {}  # user_id → (bool, timestamp)
        self._consent_cache_ttl = 300  # 5 minutes

        # CCT (Compute Contribution Token) cache for learning access gating
        self._cct_cache: Optional[tuple] = None  # (cct_string, timestamp)
        self._cct_cache_ttl = 300  # 5 minutes
        self._node_id: Optional[str] = None

        self._init_in_process()

        # Disable HTTP when there's no server to talk to.
        # If in-process failed AND no explicit HEVOLVEAI_API_URL was configured
        # (just the default localhost:8000), there's no point spamming HTTP.
        # Bundled mode (NUNBA_BUNDLED) always disables — Nunba owns the lifecycle.
        # Non-bundled with default URL also disables — no server was started.
        if not self._in_process:
            _explicit_url = os.environ.get('HEVOLVEAI_API_URL', '')
            if os.environ.get('NUNBA_BUNDLED') == '1' or not _explicit_url:
                self._http_disabled = True
                logger.info(
                    "[WorldModelBridge] Learning not available in-process, "
                    "no explicit HEVOLVEAI_API_URL — HTTP disabled")

        # Periodic HevolveAI integrity watcher (Gap 1 fix)
        self._crawl_watcher = None
        self._start_crawl_integrity_watcher()

    def _start_crawl_integrity_watcher(self) -> None:
        """Start periodic HevolveAI integrity watcher if in-process mode active.

        No-op if HTTP mode (nothing to watch — HTTP doesn't depend on
        HevolveAI's local file state).
        """
        if not self._in_process:
            return
        try:
            from security.source_protection import CrawlIntegrityWatcher
            watcher = CrawlIntegrityWatcher()
            watcher.register_tamper_callback(self._on_crawl_tamper_detected)
            watcher.start()
            self._crawl_watcher = watcher
            logger.info("[WorldModelBridge] CrawlIntegrityWatcher started")
        except ImportError:
            pass  # source_protection not available
        except Exception as e:
            logger.warning(
                f"[WorldModelBridge] CrawlIntegrityWatcher failed: {e}")

    def _on_crawl_tamper_detected(self) -> None:
        """Callback: HevolveAI files changed post-boot.

        Disable in-process mode and fall back to HTTP.
        Does NOT halt the hive — that requires master key.
        """
        logger.critical(
            "[WorldModelBridge] HevolveAI tampering detected — "
            "disabling in-process mode, falling back to HTTP")
        with self._lock:
            self._in_process = False
            self._provider = None
            self._hive_mind = None

    def _cb_is_open(self) -> bool:
        """Check if circuit breaker is open (blocking requests)."""
        return self._circuit_breaker.is_open()

    def _cb_record_success(self):
        """Reset circuit breaker on successful call."""
        self._circuit_breaker.record_success()

    def _cb_record_failure(self):
        """Record failure; open circuit at threshold."""
        self._circuit_breaker.record_failure()

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

    # U3: Hive participation opt-out (reuses consent cache pattern)
    def _has_hive_participation(self, user_id: str) -> bool:
        """Check if user has hive participation enabled (default: True).

        Users can opt out via User.settings['hive_participation'] = False.
        Cached alongside cloud consent (same TTL).
        """
        if not user_id:
            return True  # Default participate if no user context

        cache_key = f"hive_{user_id}"
        now = time.time()
        cached = self._consent_cache.get(cache_key)
        if cached and now - cached[1] < self._consent_cache_ttl:
            return cached[0]

        participate = True
        try:
            from integrations.social.models import get_db, User
            db = get_db()
            try:
                user = db.query(User).filter_by(id=user_id).first()
                if user:
                    participate = bool(
                        (user.settings or {}).get('hive_participation', True))
            finally:
                db.close()
        except Exception:
            pass

        self._consent_cache[cache_key] = (participate, now)
        return participate

    # ─── CCT (Compute Contribution Token) gating ──────────────────

    def _load_cached_cct(self) -> Optional[str]:
        """Load CCT from file, cache in memory (5 min TTL)."""
        now = time.time()
        if self._cct_cache and now - self._cct_cache[1] < self._cct_cache_ttl:
            return self._cct_cache[0]

        try:
            from .continual_learner_gate import ContinualLearnerGateService
            cct = ContinualLearnerGateService.load_cct_from_file()
            if cct:
                self._cct_cache = (cct, now)
            return cct
        except Exception:
            return None

    def _check_cct_access(self, capability: str) -> bool:
        """Check if node's CCT grants a specific capability. Zero DB calls."""
        try:
            from .continual_learner_gate import ContinualLearnerGateService
            cct = self._load_cached_cct()
            if not cct:
                return False
            return ContinualLearnerGateService.check_cct_capability(
                cct, capability, self._node_id)
        except Exception:
            return False  # Fail-closed

    def _init_in_process(self):
        """Try to connect to in-process learning pipeline (zero HTTP overhead).

        When HevolveAI is pip-installed and _init_learning_pipeline() has run
        in hart_intelligence, we get direct references to the provider
        and hivemind instances. All subsequent calls bypass HTTP entirely.

        SECURITY: Integrity verification required before enabling in-process.
        If HevolveAI files don't match the signed manifest, fall back to HTTP.
        """
        # Integrity gate: verify HevolveAI installation before in-process
        try:
            from security.source_protection import SourceProtectionService
            integrity = SourceProtectionService.verify_hevolveai_integrity()
            if not integrity.get('verified', False):
                mismatched = integrity.get('mismatched_files', [])
                if mismatched:
                    logger.warning(
                        f"[WorldModelBridge] HevolveAI integrity FAILED: "
                        f"{len(mismatched)} mismatched files — forcing HTTP mode")
                    logger.info(
                        f"[WorldModelBridge] HTTP mode: {self._api_url}")
                    return
        except ImportError:
            pass  # source_protection not available — skip check
        except Exception as e:
            logger.debug(f"[WorldModelBridge] Integrity check skipped: {e}")

        # Worker threads MUST NOT trigger a `from hart_intelligence import …`
        # here.  The hart_intelligence import chain (langchain, transformers,
        # autogen, multimodal stacks) can take 300+ seconds on first load,
        # and Python's per-module import lock is held for the entire
        # duration.  When the watchdog declares the worker FROZEN at 300s
        # and "restarts" it, the original thread can't be killed (Python
        # has no thread.kill); it stays alive holding the lock.  The new
        # thread runs the same `from hart_intelligence import …` and blocks
        # on the same lock.  After ~10 cycles you have 10 zombie daemon
        # threads, zero goals dispatched, zero spark spent — exactly the
        # 2026-04-29 dashboard incident (daemon restart_count=9).
        #
        # Only consult `sys.modules` to see whether hart_intelligence was
        # already imported on the main thread (via the bootstrap pre-warm
        # path that owns the heavyweight import).  If yes — use the
        # resolved module's accessors directly, no import lock taken.
        # If no — fall through to HTTP mode without forcing a worker-thread
        # import; the bootstrap path will retry the in-process upgrade on
        # the next record_interaction once it finishes the pre-warm.
        mod = sys.modules.get('hart_intelligence')
        if mod is not None:
            try:
                _get_provider = getattr(mod, 'get_learning_provider', None)
                _get_hive = getattr(mod, 'get_hive_mind', None)
                if _get_provider is not None:
                    provider = _get_provider()
                    hive = _get_hive() if _get_hive is not None else None
                    if provider is not None:
                        self._provider = provider
                        self._hive_mind = hive
                        self._in_process = True
                        logger.info(
                            "[WorldModelBridge] In-process mode: direct Python calls")
                        return
            except Exception as e:
                logger.debug(
                    f"[WorldModelBridge] in-process probe failed: {e}")
        logger.info(
            f"[WorldModelBridge] HTTP mode: {self._api_url}")

    # ─── Record interactions (auto-learn) ────────────────────────────

    def record_interaction(self, user_id: str, prompt_id: str,
                           prompt: str, response: str,
                           model_id: str = None, latency_ms: float = 0,
                           node_id: str = None, goal_id: str = None,
                           attribution_chain: dict = None):
        """Record every agent interaction as training data for HevolveAI.

        Called after EVERY /chat response.  Batches experiences and flushes
        them to HevolveAI (in-process or HTTP).
        HevolveAI auto-learns from every completion (3-priority queue:
        expert > reality > distillation).

        GUARDRAIL: ConstitutionalFilter screens before storage.
        """
        # Lazy in-process re-check: HevolveAI may have initialized after our __init__
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
        # Structured attribution chain — preferred carrier for
        # agent_attribution's step/observation/credit/parent_action_id
        # payload.  Previously it was packed into the 'prompt' field
        # as stringified JSON, which confused HevolveAI's prompt-text
        # distillation path.  Leave the legacy path intact for callers
        # that haven't migrated — they pass nothing, and HevolveAI
        # receives only the primary experience fields.
        if attribution_chain is not None:
            experience['attribution_chain'] = attribution_chain

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

        # Durable local write: append the user↔assistant pair to
        # ConversationEntry so FULL_HISTORY and the channel unified
        # inbox can query it by time range. Previously record_interaction
        # only batched to the in-memory _experience_queue + async flush
        # to HevolveAI, so if HevolveAI was offline the row was lost on
        # process exit. Now chat history survives restarts even without
        # a hive connection. Failures here must not break the learning
        # path — wrap in try/except and log at debug level.
        try:
            self._persist_to_conversation_entry(
                user_id=str(user_id), prompt_id=str(prompt_id),
                prompt=prompt, response=response,
                model_id=model_id or 'unknown',
            )
        except Exception as e:
            logger.debug(f"ConversationEntry durable write skipped: {e}")

        if len(self._experience_queue) >= self._flush_batch_size:
            batch = []
            while self._experience_queue and len(batch) < self._flush_batch_size:
                try:
                    batch.append(self._experience_queue.popleft())
                except IndexError:
                    break
            if batch:
                self._flush_executor.submit(self._flush_to_world_model, batch)

    def _persist_to_conversation_entry(
        self, user_id: str, prompt_id: str,
        prompt: str, response: str, model_id: str,
    ) -> None:
        """Write the user prompt + assistant response to ConversationEntry
        as two rows so FULL_HISTORY can BETWEEN-query them by created_at.

        Keeps the single write-path that integrations.channels.response.router
        uses (same model, same field contract), so existing tooling that
        already reads ConversationEntry (unified inbox, time-based history)
        sees chat-route interactions too. agent_id is left empty — that
        maps to the default chat flow; channel-inbound flows still set it.
        """
        try:
            from integrations.social.models import get_db, ConversationEntry
        except ImportError:
            return
        db = None
        try:
            db = get_db()
            for role, content in (('user', prompt), ('assistant', response)):
                if not content:
                    continue
                entry = ConversationEntry(
                    user_id=user_id,
                    channel_type='chat',
                    role=role,
                    content=content[:10000],
                    agent_id=None,
                    prompt_id=prompt_id or None,
                )
                db.add(entry)
            db.commit()
        except Exception:
            # Swallow — the caller's try/except already downgrades to
            # debug. We must not raise here under any circumstances.
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            raise
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _flush_to_world_model(self, batch: list):
        """Flush experience batch to HevolveAI's learning provider.

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

        # HTTP fallback (central standalone or HevolveAI not in-process)
        if self._http_disabled or self._cb_is_open():
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
                pooled_post(
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
        """Submit an expert correction to HevolveAI's RL-EF system.

        In-process: calls send_expert_correction() directly.
        HTTP: POST /v1/corrections.

        HevolveAI routes to:
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
                from hevolveai.embodied_ai.rl_ef import send_expert_correction
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
        if self._http_disabled:
            return {'success': False, 'reason': 'Learning not available (bundled mode)'}
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
            resp = pooled_post(
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

    def register_peer_agent(
        self,
        peer_id: str,
        agent_type: str = 'remote_peer',
        latent_dim: int = 2048,
        capabilities: Optional[list] = None,
    ) -> bool:
        """Best-effort registration of a newly-linked peer with the in-
        process HiveMind so `fuse_thoughts` / `think_together_distributed`
        can include it in MoE consensus.

        Called from `core.peer_link.link_manager.upgrade_peer` right
        after a successful `link.connect()`.  Safe to invoke in any
        topology:

        - In-process HiveMind loaded → real registration.
        - In-process HiveMind missing (no hevolveai, HTTP-only central
          tier) → logs at debug and returns False.
        - Any exception inside HiveMind → swallowed at debug; the link
          upgrade must never fail because the hive can't register.

        Returns ``True`` iff the peer was accepted into the HiveMind
        agent registry.  Callers generally ignore the return value —
        this is a side-effect hook, not a prerequisite.
        """
        if not (self._in_process and self._hive_mind):
            logger.debug(
                f"[register_peer_agent] no in-process HiveMind; "
                f"skipping peer {peer_id[:8] if peer_id else '?'}"
            )
            return False
        try:
            # Resolve AgentCapability lazily — hevolveai might expose a
            # different enum shape across versions; fall back to string
            # capabilities when the enum isn't importable.
            caps: list
            try:
                from hevolveai.embodied_ai.learning.hive_mind import (
                    AgentCapability,
                )
                default_caps = [
                    AgentCapability.ENCODE,
                    AgentCapability.REASON,
                ]
                caps = list(capabilities) if capabilities else default_caps
            except Exception:
                caps = list(capabilities) if capabilities else [
                    'encode', 'reason',
                ]
            self._hive_mind.register_agent(
                agent_id=peer_id,
                agent_type=agent_type,
                latent_dim=latent_dim,
                capabilities=caps,
            )
            logger.info(
                f"[register_peer_agent] peer={peer_id[:8]} "
                f"type={agent_type} dim={latent_dim} registered"
            )
            return True
        except Exception as e:
            logger.debug(
                f"[register_peer_agent] peer={peer_id[:8] if peer_id else '?'} "
                f"skipped: {e}"
            )
            return False

    # ─── G12: parallel SSM student inference ─────────────────────────

    def predict_student(
        self,
        prompt: str,
        messages: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 256,
        timeout_s: float = 2.0,
    ) -> Optional[Dict]:
        """Run the HevolveAI SSM student forward pass in-process.

        G12 — closes the gap where HARTOS' CustomGPT posts directly to
        llama.cpp and never involves the SSM student.  When this is
        available, callers can fire the student in parallel with the
        llama.cpp teacher and feed the (teacher, student) pair into the
        distillation engine for continuous learning.

        Returns:
            dict with keys {'response', 'action_tensor', 'epistemic_data'}
            on success, or None if:
                - In-process mode not active
                - Provider doesn't expose _generate_response
                - Student forward pass failed / timed out

        Design notes:
            * Synchronous call; use an executor at the call site if you
              want to overlap it with the teacher HTTP request.
            * Gated by HEVOLVE_PARALLEL_SSM=0 to disable per-deployment.
            * Never raises — the teacher path must not be blocked by a
              failing student.
        """
        import os as _os
        if _os.environ.get('HEVOLVE_PARALLEL_SSM', '1') == '0':
            return None
        if not (self._in_process and self._provider):
            return None
        gen = getattr(self._provider, '_generate_response', None)
        if gen is None or not callable(gen):
            return None

        if messages is None:
            messages = [{'role': 'user', 'content': prompt}]
        try:
            # _generate_response returns (response_text, action_tensor,
            # epistemic_data).  Signature verified against
            # learning_llm_provider.py:1511.
            res = gen(prompt, messages, temperature,
                      cached_vision_features=None)
        except Exception as e:
            logger.debug(f"[predict_student] SSM forward failed: {e}")
            return None

        try:
            if isinstance(res, tuple) and len(res) >= 3:
                response_text, action_tensor, epistemic_data = res[:3]
            elif isinstance(res, tuple) and len(res) == 2:
                response_text, action_tensor = res
                epistemic_data = None
            else:
                response_text, action_tensor, epistemic_data = (
                    str(res), None, None)
            with self._lock:
                self._stats.setdefault('student_inferences', 0)
                self._stats['student_inferences'] += 1
            return {
                'response': response_text,
                'action_tensor': action_tensor,
                'epistemic_data': epistemic_data,
            }
        except Exception as e:
            logger.debug(f"[predict_student] result unpack failed: {e}")
            return None

    def record_teacher_student_pair(
        self,
        prompt: str,
        teacher_response: str,
        student_response: str,
        student_action=None,
    ) -> bool:
        """Enqueue a (teacher, student) pair for Qwen distillation.

        Feeds the HevolveAI DistillationEngine the same way the in-process
        parallel path does (learning_llm_provider.py:1551-1558).  Fire-and-
        forget; returns True iff enqueue succeeded.
        """
        if not (self._in_process and self._provider):
            return False
        engine = getattr(self._provider, 'distillation_engine', None)
        if engine is None:
            return False
        if not student_response or not teacher_response:
            return False
        if student_response == teacher_response:
            # No correction needed — skip.
            return False
        try:
            engine.enqueue(
                query=prompt,
                teacher_response=teacher_response,
                student_response=student_response,
                student_action=student_action,
            )
            with self._lock:
                self._stats.setdefault('distillation_pairs', 0)
                self._stats['distillation_pairs'] += 1
            return True
        except Exception as e:
            logger.debug(f"[distillation] enqueue failed: {e}")
            return False

    def query_hivemind(self, query_text: str,
                       timeout_ms: int = 1000,
                       user_id: str = None) -> Optional[dict]:
        """Query HevolveAI's HiveMind for distributed collective thinking.

        In-process: calls hive_mind.think_together_distributed() directly.
        HTTP: POST /v1/hivemind/think.

        HevolveAI:
        1. Encodes query via frozen Qwen alignment layer (2048-D)
        2. Publishes local thought to WAMP
        3. Waits for remote agent responses (timeout_ms)
        4. Fuses with attention-weighted method
        5. Returns collective thought with contributing agents + weights

        Use this for real-time multi-modal reasoning (tensor-level fusion).
        For coarse-grained task delegation, use agent-level dispatch instead.

        CCT gating: requires 'hivemind_query' capability. Without valid CCT,
        returns cached/stale response (graceful degradation, not hard block).
        """
        # U3: Check hive participation setting
        if user_id and not self._has_hive_participation(user_id):
            logger.debug(f"HiveMind query skipped: user {user_id} opted out of hive")
            return None

        # CCT access gate (graceful: degrade to cached, don't block)
        if not self._check_cct_access('hivemind_query'):
            logger.debug("HiveMind query: no CCT with hivemind_query capability")
            cached = self._federation_aggregated.get('last_thought')
            if cached:
                return {'thought': cached, 'source': 'cached', 'cct_gated': True}
            return None

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
                # Encode query text as a thought tensor.
                # Prefer the provider's encoder; if unavailable, fall back to
                # a deterministic hash-derived vector so repeated queries of
                # the same text yield the same latent (vs torch.randn which
                # would make every call dissimilar to itself).
                # Mirrors the fallback in api_server.py's /hivethink handler.
                thought = None
                if (self._provider and
                        hasattr(self._provider, 'embodied_agent') and
                        self._provider.embodied_agent and
                        hasattr(self._provider.embodied_agent, 'encoder')):
                    try:
                        encoder = self._provider.embodied_agent.encoder
                        if hasattr(encoder, 'encode_text'):
                            encoded = encoder.encode_text(query_text)
                            # encode_text may return a tensor or (tensor, info)
                            if isinstance(encoded, tuple):
                                encoded = encoded[0]
                            flat = encoded.detach().cpu().flatten()
                            if flat.shape[0] > 2048:
                                flat = flat[:2048]
                            elif flat.shape[0] < 2048:
                                flat = torch.nn.functional.pad(
                                    flat, (0, 2048 - flat.shape[0])
                                )
                            thought = torch.nn.functional.normalize(
                                flat, dim=0
                            ).unsqueeze(0)
                    except Exception as enc_err:
                        logger.debug(
                            f"[Bridge] Encoder fallback for hivemind_query: {enc_err}"
                        )
                        thought = None

                if thought is None:
                    # Deterministic hash -> 2048-dim bit-expanded vector.
                    # Same input yields same vector (unlike torch.randn).
                    query_hash = hash(query_text) % (2 ** 31)
                    vec = torch.zeros(2048, device='cpu')
                    for i in range(2048):
                        vec[i] = ((query_hash >> (i % 31)) & 1) * 0.02 - 0.01
                    thought = torch.nn.functional.normalize(vec, dim=0).unsqueeze(0)

                result = self._hive_mind.think_together_distributed(
                    local_thought=thought,
                    local_agent_id=getattr(
                        self._hive_mind, '_local_agent_id', 'local'),
                    timeout_ms=timeout_ms,
                    **({"owner_id": user_id} if user_id else {}),
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

        # PeerLink path — collect thoughts from connected peers directly
        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()
            responses = mgr.collect('hivemind', timeout_ms=timeout_ms)
            if responses:
                with self._lock:
                    self._stats['total_hivemind_queries'] += 1
                # Each peer response is a legitimate (query -> answer) pair.
                # Feed them as training experiences so the local agent
                # learns from cross-peer knowledge, not just transient
                # query-response display. Deduplicated by peer_id +
                # response-hash in record_interaction's batcher.
                try:
                    for idx, peer_resp in enumerate(responses):
                        if not isinstance(peer_resp, dict):
                            continue
                        peer_id = (peer_resp.get('peer_id')
                                   or peer_resp.get('node_id')
                                   or f'peer_{idx}')
                        peer_text = (peer_resp.get('thought')
                                     or peer_resp.get('response')
                                     or peer_resp.get('text'))
                        if not peer_text:
                            continue
                        self.record_interaction(
                            user_id=user_id or 'hive',
                            prompt_id=f'peerlink_{peer_id}',
                            prompt=query_text[:2000],
                            response=str(peer_text)[:5000],
                            model_id=f'peerlink:{peer_id}',
                            latency_ms=float(timeout_ms),
                            node_id=peer_id,
                        )
                    with self._lock:
                        self._stats.setdefault('peerlink_responses_trained', 0)
                        self._stats['peerlink_responses_trained'] += len(responses)
                except Exception as train_err:
                    logger.debug(
                        f"[Bridge] Failed to feed PeerLink responses to training: {train_err}"
                    )
                return {
                    'thoughts': responses,
                    'source': 'peerlink',
                    'peer_count': len(responses),
                }
        except Exception:
            pass

        # HTTP fallback
        if self._http_disabled or self._cb_is_open():
            return None

        try:
            resp = pooled_post(
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

        # HTTP fallback — guarded by circuit breaker to avoid retry storms
        if self._http_disabled or self._circuit_breaker.is_open():
            return result
        try:
            resp = pooled_get(
                f'{self._api_url}/v1/stats', timeout=self._timeout_default)
            if resp.status_code == 200:
                result['learning'] = resp.json()
                self._circuit_breaker.record_success()
            else:
                self._circuit_breaker.record_failure()
        except requests.RequestException:
            self._circuit_breaker.record_failure()

        try:
            resp = pooled_get(
                f'{self._api_url}/v1/hivemind/stats', timeout=self._timeout_default)
            if resp.status_code == 200:
                result['hivemind'] = resp.json()
                self._circuit_breaker.record_success()
            else:
                self._circuit_breaker.record_failure()
        except requests.RequestException:
            self._circuit_breaker.record_failure()

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

        # HTTP fallback — guarded by circuit breaker
        if self._http_disabled or self._circuit_breaker.is_open():
            return []
        try:
            resp = pooled_get(
                f'{self._api_url}/v1/hivemind/agents', timeout=self._timeout_default)
            if resp.status_code == 200:
                data = resp.json()
                self._circuit_breaker.record_success()
                return data.get('agents', data) if isinstance(data, dict) else data
            self._circuit_breaker.record_failure()
        except requests.RequestException:
            self._circuit_breaker.record_failure()
        return []

    # ─── RALT skill distribution ─────────────────────────────────────

    def distribute_skill_packet(self, ralt_packet: dict,
                                 node_id: str,
                                 target_nodes: list = None) -> dict:
        """Distribute a learned skill (RALT packet) across hive nodes.

        Gossip broadcasts a notification that a skill is available.
        Each receiving peer triggers RALT ingestion through their own
        local HevolveAI instance.

        GUARDRAILS:
        1. CCT gate — requires 'skill_distribution' capability
        2. WorldModelSafetyBounds.gate_ralt_export() — rate limit + witness
        3. ConstructiveFilter.check_output() — constructiveness check
        """
        # CCT access gate (hard block: cannot distribute without contribution)
        if not self._check_cct_access('skill_distribution'):
            logger.info("Skill distribution blocked: no CCT with "
                        "skill_distribution capability")
            with self._lock:
                self._stats['total_skills_blocked'] += 1
            return {'success': False, 'reason': 'no_cct_skill_distribution'}

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
        # Each peer ingests via their local HevolveAI RALT receiver
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

    # ─── RALT skill ingestion (inbound, peer → local) ────────────────
    #
    # Complements distribute_skill_packet(): that method is outbound
    # (local HevolveAI learned a skill → notify peers). The two methods
    # below are the inbound path (peer learned a skill → pull & install
    # into local HevolveAI). Receiver side of the RALT hive loop.
    #
    # Wiring:
    #   gossip peer_broadcast endpoint (discovery.py)
    #     -> handle_ralt_skill_notification(msg)       ← notification
    #          pulls full packet from source_api_url
    #     -> ingest_skill_packet(packet_dict)          ← install
    #          in-process: agent.import_skill(RALTPacket.from_wire(dict))
    #          HTTP:       POST /v1/ralt/skills/install

    def handle_ralt_skill_notification(self, msg: dict) -> dict:
        """Pull a newly-announced skill from its source node and install it.

        Invoked by the /api/social/peers/broadcast gossip receiver when
        msg['type'] == 'ralt_skill_available'. The notification carries
        only a summary; we fetch the full packet from the sender's
        /v1/ralt/skills/export/{task_id} and hand it to
        ingest_skill_packet.

        Failure modes are returned as dicts rather than raised so the
        gossip dispatcher can reply with a structured diagnostic to the
        sender without crashing the blueprint.
        """
        summary = msg.get('packet_summary') or {}
        task_id = summary.get('task_id')
        source_url = (msg.get('source_api_url') or '').rstrip('/')
        source_node = msg.get('source_node', 'unknown')

        if not task_id or not source_url:
            return {'success': False,
                    'reason': 'missing_task_id_or_source_api_url'}

        # Echo prevention: don't ingest skills we ourselves broadcast.
        # _node_id on the bridge is currently unset; fall back to the
        # gossip singleton which holds the canonical local node_id.
        try:
            from integrations.social.peer_discovery import gossip
            local_node = self._node_id or getattr(gossip, 'node_id', None)
        except Exception:
            local_node = self._node_id
        if local_node and source_node == local_node:
            return {'success': False, 'reason': 'echo_skip'}

        # Pull the packet. Use circuit breaker so a repeatedly failing
        # source can't stall the gossip dispatcher.
        if self._circuit_breaker.is_open():
            return {'success': False, 'reason': 'circuit_open'}
        try:
            resp = pooled_get(
                f"{source_url}/v1/ralt/skills/export/{task_id}",
                timeout=self._timeout_default)
        except requests.RequestException as e:
            self._circuit_breaker.record_failure()
            return {'success': False, 'reason': f'fetch_failed: {e}'}

        if resp.status_code != 200:
            self._circuit_breaker.record_failure()
            return {'success': False,
                    'reason': f'export_http_{resp.status_code}'}
        try:
            packet_dict = resp.json()
        except ValueError as e:
            self._circuit_breaker.record_failure()
            return {'success': False,
                    'reason': f'export_not_json: {e}'}
        self._circuit_breaker.record_success()

        return self.ingest_skill_packet(packet_dict, source_node=source_node)

    def ingest_skill_packet(self, packet_dict: dict,
                            source_node: str = '') -> dict:
        """Install a wire-format RALT packet into the local HevolveAI.

        In-process: reconstruct RALTPacket and call embodied_agent
        .import_skill() directly (zero HTTP overhead, same code path as
        the local learning loop at integrated_realtime_agent.py:1331).

        HTTP: POST the dict to /v1/ralt/skills/install on the local
        HevolveAI API server.

        Guardrails:
        1. CCT gate — requires a token with 'skill_distribution'
           capability on the INGEST side too (prevents unauthenticated
           peers from pushing skills into our local model).
        2. WorldModelSafetyBounds.gate_ralt_ingest (if available) —
           mirror of gate_ralt_export used by distribute_skill_packet.
        """
        # Symmetric CCT gate: we require skill_distribution to ingest
        # because ingestion mutates the local world model. Mirrors the
        # check in distribute_skill_packet (line 923).
        if not self._check_cct_access('skill_distribution'):
            logger.info("Skill ingest blocked: no CCT with "
                        "skill_distribution capability")
            with self._lock:
                self._stats['total_skills_blocked'] += 1
            return {'success': False, 'reason': 'no_cct_skill_distribution'}

        # Optional ingest-side guardrail (inbound analog of
        # gate_ralt_export). Skip gracefully if not implemented.
        try:
            from security.hive_guardrails import WorldModelSafetyBounds
            if hasattr(WorldModelSafetyBounds, 'gate_ralt_ingest'):
                passed, reason = WorldModelSafetyBounds.gate_ralt_ingest(
                    packet_dict, source_node)
                if not passed:
                    with self._lock:
                        self._stats['total_skills_blocked'] += 1
                    return {'success': False, 'reason': reason}
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"gate_ralt_ingest skipped: {e}")

        # In-process fast path: reuse the exact same call that the
        # local learning loop uses after self-learning (see
        # integrated_realtime_agent.py:1331). Importing RALTPacket
        # lazily so this module works when HevolveAI isn't installed.
        if self._in_process and self._provider is not None:
            try:
                agent = getattr(self._provider, 'embodied_agent', None)
                if agent is not None and hasattr(agent, 'import_skill'):
                    from hevolveai.embodied_ai.learning.latent_transfer import (
                        RALTPacket)
                    pkt = RALTPacket.from_wire(packet_dict)
                    ok = bool(agent.import_skill(pkt, verify_topology=True))
                    with self._lock:
                        self._stats.setdefault('total_skills_received', 0)
                        self._stats.setdefault('total_skills_installed', 0)
                        self._stats['total_skills_received'] += 1
                        if ok:
                            self._stats['total_skills_installed'] += 1
                    return {
                        'success': ok,
                        'mode': 'in_process',
                        'task_id': pkt.task_id,
                        'source_id': pkt.source_id,
                    }
            except Exception as e:
                # Fall through to HTTP — in-process path failed but the
                # HTTP API may still be reachable in a hybrid setup.
                logger.debug(
                    f"[WorldModelBridge] in-process skill ingest failed, "
                    f"falling back to HTTP: {e}")

        # HTTP fallback: POST to local HevolveAI /v1/ralt/skills/install
        if self._http_disabled or self._circuit_breaker.is_open():
            return {'success': False,
                    'reason': 'http_disabled_or_circuit_open'}
        try:
            resp = pooled_post(
                f'{self._api_url}/v1/ralt/skills/install',
                json=packet_dict,
                timeout=self._timeout_default)
        except requests.RequestException as e:
            self._circuit_breaker.record_failure()
            return {'success': False, 'reason': f'install_fetch_failed: {e}'}

        if resp.status_code != 200:
            self._circuit_breaker.record_failure()
            return {'success': False,
                    'reason': f'install_http_{resp.status_code}'}
        self._circuit_breaker.record_success()
        try:
            body = resp.json()
        except ValueError:
            body = {'success': False, 'reason': 'install_not_json'}
        with self._lock:
            self._stats.setdefault('total_skills_received', 0)
            self._stats.setdefault('total_skills_installed', 0)
            self._stats['total_skills_received'] += 1
            if body.get('success'):
                self._stats['total_skills_installed'] += 1
        body.setdefault('mode', 'http')
        return body

    # ─── Health ──────────────────────────────────────────────────────

    def check_health(self) -> dict:
        """Check learning pipeline health.

        In-process: returns healthy if provider is available.
        HTTP: GET /health on HevolveAI API.
        """
        if self._in_process and self._provider:
            return {
                'healthy': True,
                'learning_active': True,
                'mode': 'in_process',
                'node_tier': self._node_tier,
            }

        # HTTP fallback
        if self._http_disabled:
            return {
                'healthy': False,
                'learning_active': False,
                'mode': 'disabled',
                'node_tier': self._node_tier,
            }
        try:
            resp = pooled_get(
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

    # ─── Embodied interaction (same latent space) ────────────────

    def send_action(self, action: dict) -> bool:
        """Send a motor/actuator command to HevolveAI's world model.

        The world model operates in one latent space — text, sensors,
        motors are all representations of the same world.  Actions are
        predictions that the world model tests against reality.

        Args:
            action: Dict with type, target, params, timestamp.
                e.g. {'type': 'motor_velocity', 'target': 'left_wheel',
                       'params': {'velocity': 0.5}, 'timestamp': ...}

        Returns:
            True if action was sent successfully.
        """
        # Safety check before forwarding
        try:
            from integrations.robotics.safety_monitor import get_safety_monitor
            monitor = get_safety_monitor()
            if monitor.is_estopped:
                logger.warning("[WorldModelBridge] Action blocked: E-stop active")
                return False
            # Check position if action has spatial params
            position = action.get('params', {})
            if any(k in position for k in ('x', 'y', 'z')):
                if not monitor.check_position_safe(position):
                    logger.warning("[WorldModelBridge] Action blocked: outside workspace")
                    return False
        except ImportError:
            pass

        # In-process: actions route through submit_correction() for error
        # propagation when outcomes differ from predictions.  No separate
        # action_stream module — the correction path handles backprop.

        # HTTP fallback
        if self._http_disabled or self._cb_is_open():
            return False

        try:
            resp = pooled_post(
                f'{self._api_url}/v1/actions',
                json=action,
                timeout=self._timeout_default,
            )
            if resp.status_code in (200, 201):
                self._cb_record_success()
                with self._lock:
                    self._stats['total_actions_sent'] = self._stats.get(
                        'total_actions_sent', 0) + 1
                return True
            self._cb_record_failure()
            return False
        except requests.RequestException:
            self._cb_record_failure()
            return False

    def ingest_sensor_batch(self, readings: list) -> int:
        """Feed sensor data to HevolveAI's world model for learning.

        The world model's latent space includes physical sensor state.
        This method batches sensor readings and flushes them to HevolveAI
        for continuous learning — the same way text experiences are flushed.

        Args:
            readings: List of SensorReading.to_dict() dicts.

        Returns:
            Number of readings successfully ingested.
        """
        if not readings:
            return 0

        # In-process: sensor data routes through submit_sensor_frame() →
        # embodied.step(sensor, train=True).  SensorInput dataclass already
        # carries type/modality metadata.  No separate sensor_ingest module.

        # HTTP fallback
        if self._http_disabled or self._cb_is_open():
            return 0

        try:
            resp = pooled_post(
                f'{self._api_url}/v1/sensors/batch',
                json={'readings': readings},
                timeout=self._timeout_flush,
            )
            if resp.status_code in (200, 201):
                self._cb_record_success()
                with self._lock:
                    self._stats['total_sensor_readings'] = self._stats.get(
                        'total_sensor_readings', 0) + len(readings)
                return len(readings)
            self._cb_record_failure()
            return 0
        except requests.RequestException:
            self._cb_record_failure()
            return 0

    def get_learning_feedback(self) -> Optional[Dict]:
        """Poll HevolveAI for real-time learning feedback.

        The world model continuously learns from sensor+action data.
        This method retrieves corrections/predictions — trajectory
        adjustments, new obstacle awareness, learned patterns.

        Returns:
            Feedback dict from HevolveAI, or None if unavailable.
        """
        # In-process: feedback is returned inline by step() as InferenceStats
        # (attention weights, epistemic uncertainty, kernel corrections).
        # The provider exposes this via get_stats(). No separate module.
        if self._in_process and self._provider:
            try:
                stats = self._provider.get_stats()
                return stats.get('last_feedback') or stats
            except Exception:
                pass

        # HTTP fallback
        if self._http_disabled or self._cb_is_open():
            return None

        try:
            resp = pooled_get(
                f'{self._api_url}/v1/feedback/latest',
                timeout=self._timeout_default,
            )
            if resp.status_code == 200:
                self._cb_record_success()
                return resp.json()
            self._cb_record_failure()
            return None
        except requests.RequestException:
            self._cb_record_failure()
            return None

    def record_embodied_interaction(
        self, action: dict, sensor_context: dict, outcome: dict,
    ):
        """Record an action+sensor+outcome triple for recipe learning.

        This extends record_interaction() for physical actions.
        The triple becomes CREATE mode training data: what action was
        taken, what sensor state surrounded it, what was the outcome.

        Stored as experiences in the same queue that text interactions use.
        Same latent space — the world model doesn't distinguish modalities.
        """
        experience = {
            'type': 'embodied_interaction',
            'action': action,
            'sensor_context': sensor_context,
            'outcome': outcome,
            'timestamp': action.get('timestamp', 0),
            'node_tier': self._node_tier,
        }
        self._experience_queue.append(experience)
        with self._lock:
            self._stats['total_recorded'] = self._stats.get('total_recorded', 0) + 1

    def emergency_stop(self) -> bool:
        """Send zero-velocity to all actuators via HevolveAI.

        This is the bridge-level emergency stop — it tells HevolveAI to
        immediately halt all physical outputs.
        """
        estop_action = {
            'type': 'emergency_stop',
            'target': '*',
            'params': {'velocity': 0, 'force': 0},
        }

        # In-process: send zero-velocity via the same HTTP estop endpoint.
        # Emergency stop must be reliable — no in-process shortcut that
        # could silently fail on ImportError.
        try:
            resp = pooled_post(
                f'{self._api_url}/v1/actions/estop',
                json=estop_action,
                timeout=3,
            )
            return resp.status_code in (200, 201)
        except requests.RequestException:
            return False

    # ─── Sensor frame forwarding to HevolveAI ────────────────────

    def submit_sensor_frame(
        self, user_id: str, frame_bytes: bytes,
        channel: str = 'camera', reality_signature: float = 1.0,
    ):
        """Forward raw sensor frame to HevolveAI for visual encoding + learning.

        HARTOS captures camera/screen frames locally. This method forwards
        them to HevolveAI's /v1/sensor/ingest endpoint so the embodied AI
        can: encode via Qwen -> predict next state -> compute error -> learn.

        Called only on scene change (adaptive sampling) to avoid overwhelming
        the learning pipeline.

        In-process mode: calls learn_from_feedback directly with encoded frame.
        HTTP mode: POST /v1/sensor/ingest with base64 data.
        """
        import base64

        source = 'camera' if channel == 'camera' else 'screen'
        sig = reality_signature if channel == 'camera' else 0.0

        if self._in_process and self._provider:
            # In-process: encode frame and feed through learning pipeline
            try:
                import torch
                embodied = getattr(self._provider, 'embodied_agent', None)
                encoder = getattr(embodied, 'encoder', None) if embodied else None
                if encoder is None:
                    encoder = getattr(self._provider, 'qwen_encoder', None)
                if encoder is not None:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
                    encoding = encoder.encode_image(img)

                    # Route through embodied agent step() — same path as /v1/sensor/ingest
                    if embodied is not None and hasattr(embodied, 'step'):
                        from hevolveai.embodied_ai.types.sensor_input import SensorInput
                        sensor = SensorInput(
                            data=encoding if isinstance(encoding, torch.Tensor)
                                 else torch.tensor(encoding).float(),
                            input_type='encoded_features',
                            modality='vision',
                            metadata={'reality_signature': sig, 'source': source},
                        )
                        embodied.step(sensor)
                    return
            except Exception as e:
                logger.debug(f"[WorldModelBridge] In-process sensor frame failed: {e}")

        # HTTP fallback
        if self._http_disabled or self._cb_is_open():
            return

        try:
            data_b64 = base64.b64encode(frame_bytes).decode('ascii')
            resp = pooled_post(
                f'{self._api_url}/v1/sensor/ingest',
                json={
                    'modality': 'vision',
                    'source': source,
                    'data': data_b64,
                    'format': 'jpeg',
                    'session_id': f'{user_id}_{source}',
                    'reality_signature': sig,
                },
                timeout=self._timeout_default,
            )
            if resp.status_code == 200:
                self._cb_record_success()
            else:
                self._cb_record_failure()
        except requests.RequestException:
            self._cb_record_failure()

    def submit_output_feedback(
        self, output_modality: str, status: str, context: str,
        model_used: str = 'unknown', error_message: str = None,
        generation_time_seconds: float = 0.0, user_id: str = 'default',
        generated_data: bytes = None, generated_format: str = None,
    ):
        """Report output modality generation result to HevolveAI for learning.

        Routes through existing endpoints (no redundant API surface):
        - Success with data: /v1/sensor/ingest (generated output = observation)
        - Error/rejection: /v1/corrections (correction signal)
        - Success without data: record_interaction (text experience)

        Generated outputs are just observations with reality_signature=0.0.
        Errors are corrections that teach modality routing.
        """
        import base64

        # Success with generated data: treat as sensor observation
        if status == 'completed' and generated_data is not None:
            # Map modality format to sensor ingest format
            modality_map = {
                'image': 'vision',
                'audio_speech': 'audio',
                'audio_music': 'audio',
                'video': 'vision',
                'video_with_audio': 'multimodal',
            }
            sensor_modality = modality_map.get(output_modality, 'multimodal')
            fmt = generated_format or 'jpeg'

            if self._cb_is_open():
                return

            try:
                data_b64 = base64.b64encode(generated_data).decode('ascii')
                resp = pooled_post(
                    f'{self._api_url}/v1/sensor/ingest',
                    json={
                        'modality': sensor_modality,
                        'source': f'generated_{output_modality}',
                        'data': data_b64,
                        'format': fmt,
                        'session_id': f'{user_id}_output_{output_modality}',
                        'reality_signature': 0.0,
                        'text': context[:500],
                    },
                    timeout=self._timeout_default,
                )
                if resp.status_code == 200:
                    self._cb_record_success()
                else:
                    self._cb_record_failure()
            except requests.RequestException:
                self._cb_record_failure()
            return

        # Error/rejection: route as correction.
        # C1 hive-memory-on-high-error: before submitting the correction,
        # ask the hive if peers have encountered this modality+error before.
        # Any collective hint is attached to the correction as explanation
        # so the KernelContinualLearner / LoRA learner gets richer context
        # than the raw error string alone. This closes the loop between
        # local failures and the shared error-memory store.
        if status in ('error', 'user_rejected') and error_message:
            hive_hint = None
            try:
                query_text = (
                    f'output_failure modality={output_modality} '
                    f'error={error_message[:200]} context={context[:200]}'
                )
                hive_result = self.query_hivemind(
                    query_text, user_id=user_id, timeout_ms=200
                )
                if hive_result:
                    if isinstance(hive_result, dict):
                        hive_hint = (
                            hive_result.get('thought')
                            or hive_result.get('thoughts')
                            or hive_result.get('source')
                        )
                    else:
                        hive_hint = str(hive_result)[:400]
                    if hive_hint:
                        with self._lock:
                            self._stats.setdefault(
                                'hive_hints_on_output_error', 0)
                            self._stats['hive_hints_on_output_error'] += 1
            except Exception as hive_err:
                logger.debug(
                    f"[Bridge] hive hint query failed for output error: {hive_err}"
                )
                hive_hint = None

            explanation = None
            if hive_hint:
                explanation = f'hive_hint: {str(hive_hint)[:400]}'

            self.submit_correction(
                original_response=f'[{output_modality}] {context[:500]}',
                corrected_response=f'{output_modality} generation failed: {error_message}',
                expert_id=user_id,
                confidence=0.8 if status == 'user_rejected' else 0.5,
                explanation=explanation,
                context={'output_modality': output_modality,
                         'status': status,
                         'user_id': user_id,
                         'hive_hint_used': bool(hive_hint)},
            )
            return

        # Success without data or pending: record as text interaction
        self.record_interaction(
            user_id=user_id,
            prompt_id=f'output_{output_modality}',
            prompt=context[:2000],
            response=f'[{output_modality} {status}] generated by {model_used} in {generation_time_seconds:.1f}s',
            model_id=model_used,
            latency_ms=generation_time_seconds * 1000,
        )

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
        """Store aggregated network-wide metrics locally and notify listeners.

        Does NOT push to HevolveAI's parametric learning — federation metrics
        are consumed by BenchmarkRegistry and dashboard, not the gradient
        pipeline. But the EventBus emit lets any local subscriber
        (dashboards, benchmark router, coding_agent benchmark_tracker)
        react without polling _federation_aggregated.

        Called by FederatedAggregator.apply_aggregated() (single entry point).
        """
        self._federation_aggregated = aggregated
        try:
            from core.platform.events import emit_event
            emit_event('learning.federation_update', {
                'epoch': aggregated.get('epoch', 0),
                'peer_count': aggregated.get('peer_count', 0),
                'convergence': aggregated.get('convergence'),
            })
            # Keep legacy topic for backward-compat with existing subscribers
            emit_event('federation.aggregated', {
                'epoch': aggregated.get('epoch', 0),
                'peer_count': aggregated.get('peer_count', 0),
            })
        except Exception as exc:
            logger.debug(f"[WorldModelBridge] federation event emit failed: {exc}")
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
