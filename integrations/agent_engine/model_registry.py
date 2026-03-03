"""
Unified Agent Goal Engine - Model Registry

Central registry of available LLM backends with speed/accuracy/cost baselines.
Distinguishes local hive models (hardware-dependent latency) from API models
(fixed baseline).  Every model call is energy-tracked and guardrail-gated.

Adding a new backend = register a ModelBackend + set env var for its API key.
"""
import math
import os
import logging
import threading
import time
from collections import deque
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')


# ─── Model Tier ───

class ModelTier(Enum):
    FAST = "fast"          # Hive compute / local models / ultrafast
    BALANCED = "balanced"  # Mid-tier API or learning models
    EXPERT = "expert"      # GPT-4, Claude, DeepSeek — slower, higher quality


# ─── Model Backend ───

class ModelBackend:
    """Represents a single LLM backend with its baselines."""

    __slots__ = (
        'model_id', 'display_name', 'tier', 'config_list_entry',
        'avg_latency_ms', 'accuracy_score', 'cost_per_1k_tokens',
        'is_local', 'hardware_dependent', 'gpu_tdp_watts',
        '_latency_samples', '_lock',
    )

    def __init__(self, model_id: str, display_name: str, tier: ModelTier,
                 config_list_entry: dict, avg_latency_ms: float = 1000.0,
                 accuracy_score: float = 0.5, cost_per_1k_tokens: float = 0.0,
                 is_local: bool = False, hardware_dependent: bool = False,
                 gpu_tdp_watts: float = 170.0):
        self.model_id = model_id
        self.display_name = display_name
        self.tier = tier
        self.config_list_entry = config_list_entry
        self.avg_latency_ms = avg_latency_ms
        self.accuracy_score = accuracy_score
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.is_local = is_local
        self.hardware_dependent = hardware_dependent
        self.gpu_tdp_watts = gpu_tdp_watts
        self._latency_samples: deque = deque(maxlen=100)
        self._lock = threading.Lock()

    def to_config_list(self) -> list:
        return [self.config_list_entry]

    def to_dict(self) -> dict:
        return {
            'model_id': self.model_id,
            'display_name': self.display_name,
            'tier': self.tier.value,
            'avg_latency_ms': self.avg_latency_ms,
            'accuracy_score': self.accuracy_score,
            'cost_per_1k_tokens': self.cost_per_1k_tokens,
            'is_local': self.is_local,
            'hardware_dependent': self.hardware_dependent,
            'gpu_tdp_watts': self.gpu_tdp_watts,
        }

    def record_latency(self, latency_ms: float):
        """Record an observed latency and update the running average."""
        with self._lock:
            self._latency_samples.append(latency_ms)
            self.avg_latency_ms = sum(self._latency_samples) / len(self._latency_samples)


# ─── Model Registry (Singleton) ───

class ModelRegistry:
    """Central registry of all available model backends.

    Every model call flows through here, so energy tracking and guardrail
    checks attach at this layer.
    """

    def __init__(self):
        self._models: Dict[str, ModelBackend] = {}
        self._lock = threading.Lock()
        self._energy_log: deque = deque(maxlen=10000)  # (timestamp, model_id, kwh)

    def register(self, backend: ModelBackend):
        """Register a model backend."""
        with self._lock:
            self._models[backend.model_id] = backend
        logger.info(f"ModelRegistry: registered {backend.model_id} "
                     f"(tier={backend.tier.value}, latency={backend.avg_latency_ms}ms, "
                     f"accuracy={backend.accuracy_score})")

    def get_model(self, model_id: str) -> Optional[ModelBackend]:
        with self._lock:
            return self._models.get(model_id)

    def get_fast_model(self, min_accuracy: float = 0.0) -> Optional[ModelBackend]:
        """Get the lowest-latency model meeting minimum accuracy."""
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if m.accuracy_score >= min_accuracy
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.avg_latency_ms)

    def get_expert_model(self, max_cost: float = float('inf')) -> Optional[ModelBackend]:
        """Get the highest-accuracy model within budget."""
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if m.cost_per_1k_tokens <= max_cost
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.accuracy_score)

    def get_local_model(self, min_accuracy: float = 0.0) -> Optional[ModelBackend]:
        """Get the highest-accuracy local model (is_local=True, cost=0).

        Used by policy-aware routing to prefer local compute for hive/idle tasks.
        """
        with self._lock:
            candidates = [m for m in self._models.values()
                          if m.is_local and m.accuracy_score >= min_accuracy]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.accuracy_score)

    def get_model_by_policy(self, policy: str = 'local_preferred',
                            task_source: str = 'own',
                            min_accuracy: float = 0.0) -> Optional[ModelBackend]:
        """Policy-aware model selection.

        Policies:
          local_only     — Only local models (is_local=True). Returns None if none available.
          local_preferred — Try local first, fall through to metered if none available.
          any            — Fastest model regardless of locality (metered costs tracked).

        For hive/idle tasks, enforces at least local_preferred unless node opted into 'any'.
        """
        if task_source in ('hive', 'idle') and policy != 'any':
            policy = 'local_preferred'

        if policy == 'local_only':
            return self.get_local_model(min_accuracy)

        if policy == 'local_preferred':
            local = self.get_local_model(min_accuracy)
            if local:
                return local
            # Fall through to metered (will be tracked + compensated)

        return self.get_fast_model(min_accuracy)

    def list_models(self, tier: ModelTier = None) -> List[ModelBackend]:
        """List all models, optionally filtered by tier."""
        with self._lock:
            models = list(self._models.values())
        if tier:
            models = [m for m in models if m.tier == tier]
        return sorted(models, key=lambda m: m.avg_latency_ms)

    def record_latency(self, model_id: str, latency_ms: float):
        """Record observed latency for a model (live running average)."""
        with self._lock:
            model = self._models.get(model_id)
        if model:
            model.record_latency(latency_ms)

    def record_energy(self, model_id: str, duration_ms: float):
        """Record energy consumption for every model call — guardrail requirement."""
        from security.hive_guardrails import EnergyAwareness
        with self._lock:
            model = self._models.get(model_id)
        if model:
            kwh = EnergyAwareness.estimate_energy_kwh(model.to_dict(), duration_ms)
            self._energy_log.append((time.time(), model_id, kwh))

    def get_total_energy_kwh(self, hours: float = 24) -> float:
        """Get total energy consumed in the last N hours."""
        cutoff = time.time() - (hours * 3600)
        return sum(kwh for ts, _, kwh in self._energy_log if ts > cutoff)

    def get_hardware_adjusted_latency(self, model_id: str,
                                       peer_node: dict = None) -> float:
        """PeerNode-aware latency estimate for hive compute nodes.

        Uses PeerNode.compute_cpu_cores / compute_ram_gb / compute_gpu_count
        to scale the baseline latency.
        """
        model = self._models.get(model_id)
        if not model:
            return float('inf')
        base = model.avg_latency_ms
        if not model.hardware_dependent or not peer_node:
            return base
        # Reference hardware: 8 cores, 16 GB RAM, 1 GPU
        gpu_factor = 1.0 / max(peer_node.get('compute_gpu_count', 1) or 1, 1)
        cpu_factor = 8.0 / max(peer_node.get('compute_cpu_cores', 8) or 8, 1)
        ram_factor = 16.0 / max(peer_node.get('compute_ram_gb', 16) or 16, 1)
        scale = 0.40 * gpu_factor + 0.35 * cpu_factor + 0.25 * ram_factor
        return base * max(scale, 0.3)  # Floor at 30% of baseline

    def update_accuracy(self, model_id: str, new_score: float):
        """Update accuracy with guardrail-enforced cap (max 5%/day improvement)."""
        from security.hive_guardrails import WorldModelSafetyBounds
        model = self._models.get(model_id)
        if model:
            capped = WorldModelSafetyBounds.gate_accuracy_update(
                model_id, model.accuracy_score, new_score)
            model.accuracy_score = capped


# ─── Module-level singleton ───
model_registry = ModelRegistry()


# ─── Default backend registration ───

def _register_defaults():
    """Register default model backends. Only available if API keys are set."""

    # 1. Local Qwen3-VL (always available — hive compute)
    model_registry.register(ModelBackend(
        model_id='qwen3-vl-4b-local',
        display_name='Qwen3-VL 4B (Local)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'Qwen3-VL-4B-Instruct',
            'api_key': 'dummy',
            'base_url': f'http://localhost:{os.environ.get("LLAMA_CPP_PORT", "8080")}/v1',
            'price': [0, 0],
        },
        avg_latency_ms=800.0,
        accuracy_score=0.55,
        cost_per_1k_tokens=0.0,
        is_local=True,
        hardware_dependent=True,
        gpu_tdp_watts=170.0,
    ))

    # 1b. Local Qwen3.5-4B text-only (always available — 256K context, llama.cpp b8148+)
    model_registry.register(ModelBackend(
        model_id='qwen3.5-4b-local',
        display_name='Qwen3.5 4B (Local)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'Qwen3.5-4B-Instruct',
            'api_key': 'dummy',
            'base_url': f'http://localhost:{os.environ.get("LLAMA_CPP_PORT", "8080")}/v1',
            'price': [0, 0],
        },
        avg_latency_ms=700.0,
        accuracy_score=0.60,
        cost_per_1k_tokens=0.0,
        is_local=True,
        hardware_dependent=True,
        gpu_tdp_watts=170.0,
    ))

    # 2. Groq (fast API — if key set)
    if os.environ.get('GROQ_API_KEY'):
        model_registry.register(ModelBackend(
            model_id='groq-llama-3.1-8b',
            display_name='Groq LLaMA 3.1 8B',
            tier=ModelTier.FAST,
            config_list_entry={
                'model': 'llama-3.1-8b-instant',
                'api_key': os.environ['GROQ_API_KEY'],
                'base_url': 'https://api.groq.com/openai/v1',
                'price': [0.05, 0.08],
            },
            avg_latency_ms=300.0,
            accuracy_score=0.60,
            cost_per_1k_tokens=0.1,
        ))

    # 3. DeepSeek V3 (balanced — if key set)
    if os.environ.get('DEEPSEEK_API_KEY'):
        model_registry.register(ModelBackend(
            model_id='deepseek-v3',
            display_name='DeepSeek V3',
            tier=ModelTier.BALANCED,
            config_list_entry={
                'model': 'deepseek-chat',
                'api_key': os.environ['DEEPSEEK_API_KEY'],
                'base_url': 'https://api.deepseek.com/v1',
                'price': [0.14, 0.28],
            },
            avg_latency_ms=1500.0,
            accuracy_score=0.82,
            cost_per_1k_tokens=0.5,
        ))

    # 4. GPT-4.1 Azure (expert — if key set)
    if os.environ.get('AZURE_OPENAI_API_KEY'):
        model_registry.register(ModelBackend(
            model_id='gpt-4.1-azure',
            display_name='GPT-4.1 (Azure)',
            tier=ModelTier.EXPERT,
            config_list_entry={
                'model': 'gpt-4.1',
                'api_type': 'azure',
                'api_key': os.environ['AZURE_OPENAI_API_KEY'],
                'base_url': os.environ.get('AZURE_OPENAI_ENDPOINT', ''),
                'api_version': '2024-12-01-preview',
                'price': [0.0025, 0.01],
            },
            avg_latency_ms=3000.0,
            accuracy_score=0.92,
            cost_per_1k_tokens=2.5,
        ))

    # 5. Claude Sonnet (expert — if key set)
    if os.environ.get('ANTHROPIC_API_KEY'):
        model_registry.register(ModelBackend(
            model_id='claude-sonnet',
            display_name='Claude Sonnet 4.5',
            tier=ModelTier.EXPERT,
            config_list_entry={
                'model': 'claude-sonnet-4-5-20250929',
                'api_key': os.environ['ANTHROPIC_API_KEY'],
                'base_url': 'https://api.anthropic.com/v1',
                'price': [0.003, 0.015],
            },
            avg_latency_ms=2500.0,
            accuracy_score=0.93,
            cost_per_1k_tokens=1.5,
        ))

    # 6. HevolveAI-Core Learning LLM (balanced — local world model, improves over time)
    hevolveai_url = os.environ.get('HEVOLVEAI_API_URL')
    if hevolveai_url:
        model_registry.register(ModelBackend(
            model_id='hevolveai-learning',
            display_name='HevolveAI World Model (Learning)',
            tier=ModelTier.BALANCED,
            config_list_entry={
                'model': 'hevolveai-learning',
                'api_key': 'local',
                'base_url': hevolveai_url,
                'price': [0, 0],
            },
            avg_latency_ms=50.0,
            accuracy_score=0.70,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
        ))

    # 7. MobileVLM ONNX (fast — lightweight CPU vision for embedded/lite tiers)
    if os.environ.get('HEVOLVE_VISION_LITE_ENABLED', '').lower() == 'true':
        model_registry.register(ModelBackend(
            model_id='mobilevlm-1.7b-onnx',
            display_name='MobileVLM 1.7B (ONNX CPU)',
            tier=ModelTier.FAST,
            config_list_entry={
                'model': 'mobilevlm-1.7b',
                'api_key': 'local',
                'base_url': 'local://onnxruntime',
                'price': [0, 0],
            },
            avg_latency_ms=500.0,
            accuracy_score=0.45,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
            gpu_tdp_watts=0.0,  # CPU-only, no GPU power draw
        ))

    # 8. Pocket TTS — offline, CPU, 100M params, MIT (always available)
    model_registry.register(ModelBackend(
        model_id='pocket-tts-100m',
        display_name='Pocket TTS 100M (Offline)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'pocket-tts-100m',
            'api_key': 'local',
            'base_url': 'inprocess://pocket_tts',
            'price': [0, 0],
        },
        avg_latency_ms=200.0,
        accuracy_score=0.85,
        cost_per_1k_tokens=0.0,
        is_local=True,
        hardware_dependent=False,
        gpu_tdp_watts=0.0,
    ))

    # 9. Whisper STT — offline, sherpa-onnx or openai-whisper (always available)
    model_registry.register(ModelBackend(
        model_id='whisper-stt-local',
        display_name='Whisper STT (sherpa-onnx / Local)',
        tier=ModelTier.FAST,
        config_list_entry={
            'model': 'whisper-stt',
            'api_key': 'local',
            'base_url': 'inprocess://whisper',
            'price': [0, 0],
        },
        avg_latency_ms=500.0,
        accuracy_score=0.88,
        cost_per_1k_tokens=0.0,
        is_local=True,
        hardware_dependent=True,
        gpu_tdp_watts=0.0,
    ))

    # 10. LuxTTS — 48kHz voice cloning TTS (GPU-accelerated, Apache 2.0)
    #     150x realtime on GPU, >1x on CPU, <1GB VRAM
    #     ZipVoice-distilled, 4-step diffusion, voice cloning from 3s audio
    _luxtts_available = False
    try:
        from zipvoice.luxvoice import LuxTTS as _LuxCheck  # noqa: F401
        _luxtts_available = True
    except ImportError:
        pass

    if _luxtts_available:
        # Detect GPU for latency estimate
        _luxtts_has_gpu = False
        try:
            import torch as _torch_check
            _luxtts_has_gpu = _torch_check.cuda.is_available()
        except ImportError:
            pass

        model_registry.register(ModelBackend(
            model_id='luxtts-48k',
            display_name='LuxTTS 48kHz (Voice Cloning)',
            tier=ModelTier.FAST,
            config_list_entry={
                'model': 'luxtts-48k',
                'api_key': 'local',
                'base_url': 'inprocess://luxtts',
                'price': [0, 0],
            },
            avg_latency_ms=50.0 if _luxtts_has_gpu else 800.0,
            accuracy_score=0.93,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
            gpu_tdp_watts=170.0 if _luxtts_has_gpu else 0.0,
        ))

    # 11. MakeItTalk Cloud — TTS + video generation (if MAKEITTALK_API_URL set)
    #     Cloud service: Flask+Celery, 7 TTS backends, lip-sync animation
    #     POST /video-gen/ for full pipeline, audio_generation for TTS only
    makeittalk_url = os.environ.get('MAKEITTALK_API_URL')
    if makeittalk_url:
        model_registry.register(ModelBackend(
            model_id='makeittalk-cloud',
            display_name='MakeItTalk Cloud (TTS + Video)',
            tier=ModelTier.BALANCED,
            config_list_entry={
                'model': 'makeittalk',
                'api_key': 'cloud',
                'base_url': makeittalk_url,
                'price': [0, 0],  # internal service
            },
            avg_latency_ms=5000.0,
            accuracy_score=0.92,
            cost_per_1k_tokens=0.0,
            is_local=False,
            hardware_dependent=False,
            gpu_tdp_watts=0.0,
        ))

    logger.info(f"ModelRegistry: {len(model_registry._models)} backends registered")


# Auto-register on import
_register_defaults()
