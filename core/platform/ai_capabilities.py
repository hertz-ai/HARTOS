"""
AI Capability Intents — Declarative AI for every app in HART OS.

Apps declare what AI they need; the OS provides it. No app bundles llama.cpp.
This is the abstraction that makes HART OS fundamentally different from
Windows/Linux/macOS.

Usage in AppManifest:
    AppManifest(
        id='translator',
        ai_capabilities=[
            AICapability(type='llm', min_accuracy=0.7).to_dict(),
            AICapability(type='tts', required=False).to_dict(),
        ],
    )

Resolution:
    router = CapabilityRouter(model_registry, vram_manager)
    result = router.resolve(AICapability(type='llm'))
    # -> ResolvedCapability(model_id='qwen3.5-4b-local', backend='local_llm', ...)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.platform')


# ─── Capability Types ───────────────────────────────────────────

class AICapabilityType(Enum):
    """All AI capability types the OS can provide."""
    LLM = 'llm'
    VISION = 'vision'
    TTS = 'tts'
    STT = 'stt'
    IMAGE_GEN = 'image_gen'
    EMBEDDING = 'embedding'
    CODE = 'code'


# ─── Capability Declaration ─────────────────────────────────────

@dataclass
class AICapability:
    """A single AI capability an app needs from the OS.

    Declarative: the app says WHAT it needs, not HOW to provide it.
    The CapabilityRouter resolves this to a concrete model backend.
    """
    type: str                                    # AICapabilityType value
    required: bool = True                        # False = nice-to-have
    local_only: bool = False                     # Never route to cloud
    min_accuracy: float = 0.0                    # 0.0-1.0 quality threshold
    max_latency_ms: float = 0.0                  # 0 = no constraint
    max_cost_spark: float = 0.0                  # 0 = no constraint
    options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for AppManifest storage."""
        return {
            'type': self.type,
            'required': self.required,
            'local_only': self.local_only,
            'min_accuracy': self.min_accuracy,
            'max_latency_ms': self.max_latency_ms,
            'max_cost_spark': self.max_cost_spark,
            'options': self.options,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AICapability':
        """Deserialize from dict."""
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


# ─── Resolved Capability ────────────────────────────────────────

@dataclass
class ResolvedCapability:
    """Result of resolving an AICapability to a concrete backend."""
    capability_type: str
    model_id: str
    backend: str                   # 'local_llm', 'mesh', 'cloud_tts', etc.
    is_local: bool
    estimated_latency_ms: float
    estimated_cost_spark: float
    available: bool
    reason: str = ''               # Why unavailable, if not available

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API responses."""
        return {
            'capability_type': self.capability_type,
            'model_id': self.model_id,
            'backend': self.backend,
            'is_local': self.is_local,
            'estimated_latency_ms': self.estimated_latency_ms,
            'estimated_cost_spark': self.estimated_cost_spark,
            'available': self.available,
            'reason': self.reason,
        }


# ─── Capability type → model type mapping ───────────────────────

_CAPABILITY_MODEL_MAP = {
    'llm': 'llm',
    'vision': 'vision',
    'tts': 'tts',
    'stt': 'stt',
    'image_gen': 'image_gen',
    'embedding': 'llm',
    'code': 'llm',
}


# ─── Capability Router ──────────────────────────────────────────

class CapabilityRouter:
    """Resolves AI capability intents to concrete model backends.

    Uses existing ModelRegistry for backend selection and VRAMManager
    for resource-aware routing. Pure resolver — no state, no side effects
    beyond event emission.
    """

    def __init__(self, model_registry=None, vram_manager=None):
        self._model_registry = model_registry
        self._vram_manager = vram_manager

    def resolve(self, capability: AICapability) -> ResolvedCapability:
        """Resolve a single capability to the best available backend.

        Maps capability constraints to ModelRegistry.get_model_by_policy():
        - local_only -> policy='local_only'
        - min_accuracy > 0.7 -> prefer EXPERT tier
        - max_cost_spark == 0 -> policy='local_only' (free models only)
        """
        cap_type = capability.type
        unavailable = ResolvedCapability(
            capability_type=cap_type, model_id='', backend='',
            is_local=False, estimated_latency_ms=0, estimated_cost_spark=0,
            available=False,
        )

        if not self._model_registry:
            unavailable.reason = 'no model registry available'
            self._emit_unavailable(cap_type, unavailable.reason)
            return unavailable

        # Determine policy from constraints
        if capability.local_only or capability.max_cost_spark == 0:
            policy = 'local_only'
        elif capability.min_accuracy >= 0.7:
            policy = 'any'  # allow cloud for high quality
        else:
            policy = 'local_preferred'

        # Find matching model
        try:
            model = self._model_registry.get_model_by_policy(
                policy=policy,
                min_accuracy=capability.min_accuracy,
            )
        except Exception:
            model = None

        if not model:
            unavailable.reason = f'no {cap_type} model matching policy={policy}'
            self._emit_unavailable(cap_type, unavailable.reason)
            return unavailable

        # Check latency constraint
        if (capability.max_latency_ms > 0 and
                model.avg_latency_ms > capability.max_latency_ms):
            unavailable.reason = (
                f'best model {model.model_id} latency {model.avg_latency_ms}ms '
                f'exceeds max {capability.max_latency_ms}ms'
            )
            self._emit_unavailable(cap_type, unavailable.reason)
            return unavailable

        # Check cost constraint
        if (capability.max_cost_spark > 0 and
                model.cost_per_1k_tokens > capability.max_cost_spark):
            unavailable.reason = (
                f'model {model.model_id} cost {model.cost_per_1k_tokens} '
                f'exceeds max {capability.max_cost_spark}'
            )
            self._emit_unavailable(cap_type, unavailable.reason)
            return unavailable

        # Check VRAM if local model
        if model.is_local and self._vram_manager:
            try:
                gpu = self._vram_manager.detect_gpu()
                if gpu and gpu.get('free_mb', 0) < 500:
                    # Low VRAM — still available but note it
                    logger.debug("Low VRAM for %s, model may use CPU offload",
                                 model.model_id)
            except Exception:
                pass

        backend = 'local' if model.is_local else 'cloud'
        resolved = ResolvedCapability(
            capability_type=cap_type,
            model_id=model.model_id,
            backend=backend,
            is_local=model.is_local,
            estimated_latency_ms=model.avg_latency_ms,
            estimated_cost_spark=model.cost_per_1k_tokens,
            available=True,
        )

        self._emit_resolved(cap_type, resolved)
        return resolved

    def resolve_all(self, capabilities: List[AICapability]) -> List[ResolvedCapability]:
        """Resolve all capabilities for an app."""
        return [self.resolve(cap) for cap in capabilities]

    def can_satisfy(self, capabilities: List[AICapability]) -> bool:
        """Check if all required capabilities can be satisfied."""
        for cap in capabilities:
            if cap.required:
                resolved = self.resolve(cap)
                if not resolved.available:
                    return False
        return True

    def _emit_resolved(self, cap_type: str, resolved: ResolvedCapability) -> None:
        """Emit capability.resolved event (best-effort)."""
        try:
            from core.platform.events import emit_event
            emit_event('capability.resolved', {
                'type': cap_type,
                'model_id': resolved.model_id,
                'is_local': resolved.is_local,
            })
        except Exception:
            pass

    def _emit_unavailable(self, cap_type: str, reason: str) -> None:
        """Emit capability.unavailable event (best-effort)."""
        try:
            from core.platform.events import emit_event
            emit_event('capability.unavailable', {
                'type': cap_type,
                'reason': reason,
            })
        except Exception:
            pass

    def health(self) -> dict:
        """Health report for ServiceRegistry."""
        return {
            'status': 'ok',
            'has_model_registry': self._model_registry is not None,
            'has_vram_manager': self._vram_manager is not None,
        }
