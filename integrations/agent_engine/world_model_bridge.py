"""
Unified Agent Goal Engine - World Model Bridge

Bridge between LLM-langchain orchestration and crawl4ai's embodied AI.
Every agent interaction becomes training data via crawl4ai's real endpoints.
Skills distribute via gossip notification + local RALT ingestion.

crawl4ai API (FastAPI on port 8000):
  POST /v1/chat/completions  — OpenAI-compatible, auto-learns from every interaction
  POST /v1/corrections       — Expert feedback (RL-EF), factual (kernel) or conceptual (LoRA)
  POST /v1/hivemind/think    — Distributed collective thinking (tensor fusion)
  GET  /v1/stats             — Learning statistics
  GET  /v1/hivemind/stats    — Collective intelligence stats
  GET  /v1/hivemind/agents   — Connected agents list
  GET  /health               — Health check

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

    Records every interaction as training data via /v1/chat/completions,
    submits expert corrections via /v1/corrections, queries the hive
    via /v1/hivemind/think, and monitors learning via /v1/stats.

    Two-tier thinking model:
    - Agent-level: Hevolve dispatches coarse-grained goals (task delegation)
    - Tensor-level: crawl4ai fuses heterogeneous agent thoughts (HiveMind)
    This bridge connects the two layers.
    """

    def __init__(self):
        self._api_url = os.environ.get(
            'CRAWL4AI_API_URL', 'http://localhost:8000')
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
        }

    # ─── Record interactions (auto-learn) ────────────────────────────

    def record_interaction(self, user_id: str, prompt_id: str,
                           prompt: str, response: str,
                           model_id: str = None, latency_ms: float = 0,
                           node_id: str = None, goal_id: str = None):
        """Record every agent interaction as training data for crawl4ai.

        Called after EVERY /chat response.  Batches experiences and flushes
        them to crawl4ai's POST /v1/chat/completions in OpenAI format.
        crawl4ai auto-learns from every completion (3-priority queue:
        expert > reality > distillation).

        GUARDRAIL: ConstitutionalFilter screens before storage.
        """
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
        """Flush experience batch to crawl4ai via POST /v1/chat/completions.

        Each experience is sent as an OpenAI-format chat completion.
        crawl4ai's LearningLLMProvider auto-learns from every request:
        - Records to trace_recorder (offline analysis)
        - Feeds distillation queue (background Qwen teacher→student)
        - Updates embodied agent's episodic memory
        """
        for exp in batch:
            try:
                body = {
                    'model': 'hevolve-interaction-replay',
                    'messages': [
                        {
                            'role': 'system',
                            'content': json.dumps({
                                'source': exp.get('source', 'langchain_orchestration'),
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
                    timeout=15,
                )
                with self._lock:
                    self._stats['total_flushed'] += 1
            except requests.RequestException:
                pass
            except Exception as e:
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

        Calls POST /v1/corrections.  crawl4ai routes to:
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

        try:
            resp = requests.post(
                f'{self._api_url}/v1/corrections',
                json=body,
                timeout=30,
            )
            if resp.status_code == 200:
                with self._lock:
                    self._stats['total_corrections'] += 1
                return resp.json()
            return {'success': False, 'reason': f'HTTP {resp.status_code}'}
        except requests.RequestException as e:
            logger.debug(f"Correction submission failed: {e}")
            return {'success': False, 'reason': str(e)}

    # ─── HiveMind collective thinking ────────────────────────────────

    def query_hivemind(self, query_text: str,
                       timeout_ms: int = 1000) -> Optional[dict]:
        """Query crawl4ai's HiveMind for distributed collective thinking.

        Calls POST /v1/hivemind/think.  crawl4ai:
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

        try:
            resp = requests.post(
                f'{self._api_url}/v1/hivemind/think',
                json={'query': query_text[:2000], 'timeout_ms': timeout_ms},
                timeout=max(30, timeout_ms / 1000 + 5),
            )
            if resp.status_code == 200:
                with self._lock:
                    self._stats['total_hivemind_queries'] += 1
                return resp.json()
        except requests.RequestException as e:
            logger.debug(f"HiveMind query failed: {e}")
        return None

    # ─── Learning stats ──────────────────────────────────────────────

    def get_learning_stats(self) -> dict:
        """Get merged learning + hivemind + bridge stats.

        Calls GET /v1/stats (learning) and GET /v1/hivemind/stats (collective).
        Returns combined dict for dashboard consumption.
        """
        result = {'learning': {}, 'hivemind': {}, 'bridge': self.get_stats()}

        try:
            resp = requests.get(
                f'{self._api_url}/v1/stats', timeout=10)
            if resp.status_code == 200:
                result['learning'] = resp.json()
        except requests.RequestException:
            pass

        try:
            resp = requests.get(
                f'{self._api_url}/v1/hivemind/stats', timeout=10)
            if resp.status_code == 200:
                result['hivemind'] = resp.json()
        except requests.RequestException:
            pass

        return result

    def get_hivemind_agents(self) -> list:
        """Get list of connected HiveMind agents from crawl4ai.

        Calls GET /v1/hivemind/agents.  Returns agent specs with
        capabilities, modality, latent dimensions, accuracy.
        """
        try:
            resp = requests.get(
                f'{self._api_url}/v1/hivemind/agents', timeout=10)
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
        """Check crawl4ai API health.

        Calls GET /health.  Returns {healthy: bool, details: {...}}.
        """
        try:
            resp = requests.get(
                f'{self._api_url}/health', timeout=5)
            if resp.status_code == 200:
                data = resp.json() if resp.headers.get(
                    'content-type', '').startswith('application/json') else {}
                return {'healthy': True, 'details': data}
            return {'healthy': False, 'details': {'status_code': resp.status_code}}
        except requests.RequestException as e:
            return {'healthy': False, 'details': {'error': str(e)}}

    def get_stats(self) -> dict:
        """Get bridge-level statistics."""
        with self._lock:
            return {
                'queue_size': len(self._experience_queue),
                'api_url': self._api_url,
                **self._stats,
            }


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
