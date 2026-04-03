"""
ProviderRegistry — catalog of all compute providers and business services.

Each provider has:
  - API endpoint format + auth method
  - Supported models + capabilities
  - Pricing (per token / per second / per image / flat)
  - Latency/throughput from efficiency matrix
  - Commission rate (for affiliate services)
  - Health status (last check, error rate)

JSON-persisted at ~/Documents/Nunba/data/provider_registry.json.
Thread-safe singleton via get_registry().
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Provider types
# ═══════════════════════════════════════════════════════════════════════

PROVIDER_TYPE_API = 'api'           # Raw API — we call it, we pay
PROVIDER_TYPE_AFFILIATE = 'affiliate'  # Business service — redirect, earn commission
PROVIDER_TYPE_LOCAL = 'local'       # Local model (llama.cpp, torch, piper)

# Auth methods
AUTH_BEARER = 'bearer'              # Authorization: Bearer <key>
AUTH_HEADER = 'header'              # Custom header (e.g. x-api-key)
AUTH_QUERY = 'query'                # API key in query string
AUTH_NONE = 'none'                  # No auth (local, free tiers)

# Pricing units
PRICE_PER_1K_TOKENS = 'per_1k_tokens'
PRICE_PER_1M_TOKENS = 'per_1m_tokens'
PRICE_PER_SECOND = 'per_second'     # Video/audio generation
PRICE_PER_IMAGE = 'per_image'
PRICE_PER_REQUEST = 'per_request'
PRICE_FLAT_MONTHLY = 'flat_monthly'
PRICE_FREE = 'free'


@dataclass
class ProviderModel:
    """A model available on a specific provider."""
    model_id: str               # Provider's model ID (e.g. "meta/llama-3.1-70b")
    canonical_id: str = ''      # Our catalog ID for cross-provider matching
    model_type: str = 'llm'     # llm, tts, stt, vlm, image_gen, video_gen, etc.
    context_length: int = 0
    max_output_tokens: int = 0

    # Pricing (provider-specific)
    input_price: float = 0.0    # Cost per pricing_unit for input
    output_price: float = 0.0   # Cost per pricing_unit for output
    pricing_unit: str = PRICE_PER_1M_TOKENS

    # Capabilities
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False
    max_images: int = 0         # For image/video gen: max batch size
    max_duration_s: float = 0   # For video/audio gen: max duration

    # Performance (populated by efficiency matrix)
    avg_tok_per_s: float = 0.0
    avg_latency_ms: float = 0.0
    quality_score: float = 0.5  # 0-1, from benchmarks
    reliability: float = 1.0    # 0-1, success rate

    enabled: bool = True


@dataclass
class Provider:
    """A compute provider or business service."""

    # ── Identity ─────────────────────────────────────────────────────
    id: str                         # Unique slug: "together", "replicate", "seedance"
    name: str                       # Display name: "Together AI"
    provider_type: str = PROVIDER_TYPE_API  # api, affiliate, local
    url: str = ''                   # Base URL or website
    docs_url: str = ''              # API docs URL
    logo_url: str = ''              # Logo for UI

    # ── API Configuration ────────────────────────────────────────────
    base_url: str = ''              # API base: "https://api.together.xyz/v1"
    api_format: str = 'openai'      # openai, replicate, custom
    auth_method: str = AUTH_BEARER
    auth_header: str = 'Authorization'  # Header name for custom auth
    env_key: str = ''               # Env var for API key: "TOGETHER_API_KEY"

    # ── Models ───────────────────────────────────────────────────────
    models: Dict[str, ProviderModel] = field(default_factory=dict)

    # ── Affiliate / Commission ───────────────────────────────────────
    affiliate_url: str = ''         # Affiliate signup URL
    affiliate_tag: str = ''         # Our affiliate ID/tag
    commission_pct: float = 0.0     # Commission percentage (e.g. 20.0 = 20%)
    commission_type: str = ''       # 'recurring', 'one_time', 'revenue_share'
    referral_url_template: str = '' # Template: "https://site.com/?ref={tag}"

    # ── Health ───────────────────────────────────────────────────────
    healthy: bool = True
    last_health_check: float = 0.0
    error_rate_24h: float = 0.0
    avg_latency_ms: float = 0.0

    # ── State ────────────────────────────────────────────────────────
    enabled: bool = True
    api_key_set: bool = False       # Whether user has configured API key

    # ── Tags ─────────────────────────────────────────────────────────
    tags: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)  # "llm", "image", "video", etc.

    def to_dict(self) -> dict:
        d = asdict(self)
        d['models'] = {k: asdict(v) for k, v in self.models.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Provider':
        models_raw = d.pop('models', {})
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        p = cls(**filtered)
        for k, v in models_raw.items():
            if isinstance(v, dict):
                pm_known = {f.name for f in ProviderModel.__dataclass_fields__.values()}
                p.models[k] = ProviderModel(**{fk: fv for fk, fv in v.items() if fk in pm_known})
        return p

    def get_api_key(self) -> str:
        """Resolve API key from env var."""
        if not self.env_key:
            return ''
        return os.environ.get(self.env_key, '')

    def has_api_key(self) -> bool:
        return bool(self.get_api_key())


# ═══════════════════════════════════════════════════════════════════════
# Built-in providers — raw API providers with OpenAI-compatible endpoints
# ═══════════════════════════════════════════════════════════════════════

def _builtin_providers() -> List[Provider]:
    """Return the built-in provider catalog.

    Pricing as of 2025-Q2.  Updated by efficiency matrix at runtime.
    """
    return [
        # ── LLM / Multi-modal API providers ──────────────────────────
        Provider(
            id='together', name='Together AI',
            provider_type=PROVIDER_TYPE_API,
            url='https://together.ai',
            base_url='https://api.together.xyz/v1',
            api_format='openai',
            env_key='TOGETHER_API_KEY',
            categories=['llm', 'image_gen', 'embedding'],
            tags=['fast', 'cheap', 'openai-compatible'],
            models={
                'meta-llama/Llama-3.3-70B-Instruct-Turbo': ProviderModel(
                    model_id='meta-llama/Llama-3.3-70B-Instruct-Turbo',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.88, output_price=0.88,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True, supports_streaming=True,
                ),
                'meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8': ProviderModel(
                    model_id='meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8',
                    canonical_id='llama-4-maverick', model_type='llm',
                    context_length=1048576, max_output_tokens=4096,
                    input_price=0.27, output_price=0.85,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True, supports_vision=True,
                ),
                'Qwen/Qwen2.5-72B-Instruct-Turbo': ProviderModel(
                    model_id='Qwen/Qwen2.5-72B-Instruct-Turbo',
                    canonical_id='qwen2.5-72b', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=1.20, output_price=1.20,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
                'deepseek-ai/DeepSeek-V3': ProviderModel(
                    model_id='deepseek-ai/DeepSeek-V3',
                    canonical_id='deepseek-v3', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.90, output_price=0.90,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
            },
        ),

        Provider(
            id='fireworks', name='Fireworks AI',
            provider_type=PROVIDER_TYPE_API,
            url='https://fireworks.ai',
            base_url='https://api.fireworks.ai/inference/v1',
            api_format='openai',
            env_key='FIREWORKS_API_KEY',
            categories=['llm', 'image_gen', 'embedding'],
            tags=['fast', 'cheap', 'openai-compatible', 'function-calling'],
            models={
                'accounts/fireworks/models/llama-v3p3-70b-instruct': ProviderModel(
                    model_id='accounts/fireworks/models/llama-v3p3-70b-instruct',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.90, output_price=0.90,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True, supports_json_mode=True,
                ),
                'accounts/fireworks/models/deepseek-v3': ProviderModel(
                    model_id='accounts/fireworks/models/deepseek-v3',
                    canonical_id='deepseek-v3', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.90, output_price=0.90,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
            },
        ),

        Provider(
            id='groq', name='Groq',
            provider_type=PROVIDER_TYPE_API,
            url='https://groq.com',
            base_url='https://api.groq.com/openai/v1',
            api_format='openai',
            env_key='GROQ_API_KEY',
            categories=['llm'],
            tags=['fastest', 'openai-compatible', 'free-tier'],
            models={
                'llama-3.3-70b-versatile': ProviderModel(
                    model_id='llama-3.3-70b-versatile',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=131072, max_output_tokens=32768,
                    input_price=0.59, output_price=0.79,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
                'deepseek-r1-distill-llama-70b': ProviderModel(
                    model_id='deepseek-r1-distill-llama-70b',
                    canonical_id='deepseek-r1-70b', model_type='llm',
                    context_length=131072, max_output_tokens=16384,
                    input_price=0.75, output_price=0.99,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                ),
                'llama-3.2-90b-vision-preview': ProviderModel(
                    model_id='llama-3.2-90b-vision-preview',
                    canonical_id='llama-3.2-90b-vision', model_type='vlm',
                    context_length=8192, max_output_tokens=8192,
                    input_price=0.90, output_price=0.90,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_vision=True,
                ),
            },
        ),

        Provider(
            id='deepinfra', name='DeepInfra',
            provider_type=PROVIDER_TYPE_API,
            url='https://deepinfra.com',
            base_url='https://api.deepinfra.com/v1/openai',
            api_format='openai',
            env_key='DEEPINFRA_API_KEY',
            categories=['llm', 'embedding', 'image_gen'],
            tags=['cheap', 'openai-compatible'],
            models={
                'meta-llama/Llama-3.3-70B-Instruct-Turbo': ProviderModel(
                    model_id='meta-llama/Llama-3.3-70B-Instruct-Turbo',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.35, output_price=0.40,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
                'Qwen/QwQ-32B': ProviderModel(
                    model_id='Qwen/QwQ-32B',
                    canonical_id='qwq-32b', model_type='llm',
                    context_length=131072, max_output_tokens=32768,
                    input_price=0.15, output_price=0.60,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
            },
        ),

        Provider(
            id='cerebras', name='Cerebras',
            provider_type=PROVIDER_TYPE_API,
            url='https://cerebras.ai',
            base_url='https://api.cerebras.ai/v1',
            api_format='openai',
            env_key='CEREBRAS_API_KEY',
            categories=['llm'],
            tags=['fastest-inference', 'openai-compatible'],
            models={
                'llama-3.3-70b': ProviderModel(
                    model_id='llama-3.3-70b',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=8192, max_output_tokens=8192,
                    input_price=0.60, output_price=0.60,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_tools=True,
                ),
            },
        ),

        Provider(
            id='sambanova', name='SambaNova',
            provider_type=PROVIDER_TYPE_API,
            url='https://sambanova.ai',
            base_url='https://api.sambanova.ai/v1',
            api_format='openai',
            env_key='SAMBANOVA_API_KEY',
            categories=['llm'],
            tags=['fast', 'openai-compatible', 'free-tier'],
            models={
                'Meta-Llama-3.3-70B-Instruct': ProviderModel(
                    model_id='Meta-Llama-3.3-70B-Instruct',
                    canonical_id='llama-3.3-70b', model_type='llm',
                    context_length=131072, max_output_tokens=4096,
                    input_price=0.60, output_price=0.60,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                ),
                'DeepSeek-R1': ProviderModel(
                    model_id='DeepSeek-R1',
                    canonical_id='deepseek-r1', model_type='llm',
                    context_length=131072, max_output_tokens=16384,
                    input_price=2.50, output_price=10.00,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                ),
            },
        ),

        Provider(
            id='openrouter', name='OpenRouter',
            provider_type=PROVIDER_TYPE_API,
            url='https://openrouter.ai',
            base_url='https://openrouter.ai/api/v1',
            api_format='openai',
            env_key='OPENROUTER_API_KEY',
            categories=['llm', 'vlm', 'image_gen'],
            tags=['aggregator', 'openai-compatible', 'all-models'],
            commission_pct=0.0,  # OpenRouter takes margin, we use as fallback
        ),

        Provider(
            id='replicate', name='Replicate',
            provider_type=PROVIDER_TYPE_API,
            url='https://replicate.com',
            base_url='https://api.replicate.com/v1',
            api_format='replicate',
            env_key='REPLICATE_API_TOKEN',
            categories=['llm', 'image_gen', 'video_gen', 'audio_gen', 'vlm'],
            tags=['serverless', 'gpu-on-demand', 'all-model-types'],
            models={
                'meta/meta-llama-3-70b-instruct': ProviderModel(
                    model_id='meta/meta-llama-3-70b-instruct',
                    canonical_id='llama-3-70b', model_type='llm',
                    input_price=0.65, output_price=2.75,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                ),
                'black-forest-labs/flux-1.1-pro': ProviderModel(
                    model_id='black-forest-labs/flux-1.1-pro',
                    canonical_id='flux-1.1-pro', model_type='image_gen',
                    input_price=0.04, output_price=0.0,
                    pricing_unit=PRICE_PER_IMAGE,
                ),
                'minimax/video-01': ProviderModel(
                    model_id='minimax/video-01',
                    canonical_id='video-01', model_type='video_gen',
                    input_price=0.25, output_price=0.0,
                    pricing_unit=PRICE_PER_SECOND,
                    max_duration_s=6,
                ),
            },
        ),

        Provider(
            id='fal', name='fal.ai',
            provider_type=PROVIDER_TYPE_API,
            url='https://fal.ai',
            base_url='https://fal.run',
            api_format='custom',
            auth_method=AUTH_HEADER,
            auth_header='Authorization',
            env_key='FAL_KEY',
            categories=['image_gen', 'video_gen', 'audio_gen', 'llm'],
            tags=['serverless', 'fast', 'media-generation'],
            models={
                'fal-ai/flux-pro/v1.1': ProviderModel(
                    model_id='fal-ai/flux-pro/v1.1',
                    canonical_id='flux-1.1-pro', model_type='image_gen',
                    input_price=0.04, output_price=0.0,
                    pricing_unit=PRICE_PER_IMAGE,
                ),
                'fal-ai/minimax/video-01': ProviderModel(
                    model_id='fal-ai/minimax/video-01',
                    canonical_id='video-01', model_type='video_gen',
                    input_price=0.50, output_price=0.0,
                    pricing_unit=PRICE_PER_SECOND,
                    max_duration_s=6,
                ),
                'fal-ai/stable-audio': ProviderModel(
                    model_id='fal-ai/stable-audio',
                    canonical_id='stable-audio', model_type='audio_gen',
                    input_price=0.04, output_price=0.0,
                    pricing_unit=PRICE_PER_REQUEST,
                ),
            },
        ),

        Provider(
            id='huggingface', name='HuggingFace Inference',
            provider_type=PROVIDER_TYPE_API,
            url='https://huggingface.co',
            base_url='https://api-inference.huggingface.co',
            api_format='custom',
            env_key='HF_TOKEN',
            categories=['llm', 'image_gen', 'embedding', 'stt', 'tts'],
            tags=['free-tier', 'all-model-types', 'open-source'],
        ),

        # ── Affiliate / Commission providers ─────────────────────────

        Provider(
            id='runwayml', name='RunwayML',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://runwayml.com',
            categories=['video_gen', 'image_gen'],
            tags=['video', 'professional', 'gen-3'],
            affiliate_url='https://runwayml.com/affiliates',
            commission_pct=20.0,
            commission_type='recurring',
            referral_url_template='https://runwayml.com/?ref={tag}',
        ),

        Provider(
            id='elevenlabs', name='ElevenLabs',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://elevenlabs.io',
            categories=['tts', 'audio_gen', 'stt'],
            tags=['voice', 'tts', 'voice-cloning'],
            affiliate_url='https://elevenlabs.io/affiliates',
            commission_pct=22.0,
            commission_type='recurring',
            referral_url_template='https://elevenlabs.io/?via={tag}',
        ),

        Provider(
            id='midjourney', name='Midjourney',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://midjourney.com',
            categories=['image_gen'],
            tags=['image', 'art', 'creative'],
            commission_pct=0.0,  # No public affiliate program
        ),

        Provider(
            id='pika', name='Pika',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://pika.art',
            categories=['video_gen'],
            tags=['video', 'consumer'],
            commission_pct=0.0,
        ),

        Provider(
            id='kling', name='Kling AI',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://klingai.com',
            categories=['video_gen', 'image_gen'],
            tags=['video', 'chinese-ai'],
            commission_pct=0.0,
        ),

        Provider(
            id='luma', name='Luma AI',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://lumalabs.ai',
            categories=['video_gen', '3d_gen'],
            tags=['video', 'dream-machine', '3d'],
            commission_pct=0.0,
        ),

        Provider(
            id='seedance', name='Seedance AI',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://www.seedance2ai.io',
            categories=['video_gen', 'image_gen', 'audio_gen'],
            tags=['video', 'multi-model', 'cheap'],
            commission_pct=0.0,
        ),

        Provider(
            id='sora', name='OpenAI Sora',
            provider_type=PROVIDER_TYPE_AFFILIATE,
            url='https://sora.com',
            categories=['video_gen'],
            tags=['video', 'openai'],
            commission_pct=0.0,
        ),

        # ── Local provider (built-in) ────────────────────────────────

        Provider(
            id='local', name='Local (on-device)',
            provider_type=PROVIDER_TYPE_LOCAL,
            url='',
            base_url='http://localhost:8080',
            api_format='openai',
            categories=['llm', 'tts', 'stt', 'vlm', 'image_gen'],
            tags=['free', 'private', 'offline', 'no-api-key'],
            enabled=True,
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════

class ProviderRegistry:
    """Central catalog of all providers. JSON-persisted, thread-safe."""

    def __init__(self, registry_path: Optional[str] = None):
        try:
            from core.platform_paths import get_db_dir
            data_dir = Path(get_db_dir())
        except ImportError:
            data_dir = Path.home() / 'Documents' / 'Nunba' / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)

        self._path = Path(registry_path) if registry_path else data_dir / 'provider_registry.json'
        self._providers: Dict[str, Provider] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load from JSON, merge with builtins."""
        # Start with builtins
        for p in _builtin_providers():
            self._providers[p.id] = p

        # Overlay user customizations from JSON
        if self._path.exists():
            try:
                with open(self._path, 'r') as f:
                    data = json.load(f)
                for pid, pdata in data.items():
                    if pid in self._providers:
                        # Merge user config over builtin (preserve API keys, enable state)
                        existing = self._providers[pid]
                        for k, v in pdata.items():
                            if k == 'models':
                                for mk, mv in v.items():
                                    if isinstance(mv, dict):
                                        pm_known = {fn.name for fn in ProviderModel.__dataclass_fields__.values()}
                                        existing.models[mk] = ProviderModel(
                                            **{fk: fv for fk, fv in mv.items() if fk in pm_known})
                            elif hasattr(existing, k):
                                setattr(existing, k, v)
                    else:
                        # User-added provider
                        self._providers[pid] = Provider.from_dict(pdata)
                logger.info("Provider registry loaded: %d providers (%s)",
                            len(self._providers), self._path)
            except Exception as e:
                logger.warning("Failed to load provider registry: %s", e)

    def save(self):
        """Persist to JSON."""
        with self._lock:
            data = {}
            for pid, p in self._providers.items():
                data[pid] = p.to_dict()
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, 'w') as f:
                    json.dump(data, f, indent=2, default=str)
            except Exception as e:
                logger.error("Failed to save provider registry: %s", e)

    # ── Query API ─────────────────────────────────────────────────────

    def get(self, provider_id: str) -> Optional[Provider]:
        return self._providers.get(provider_id)

    def list_all(self) -> List[Provider]:
        return list(self._providers.values())

    def list_enabled(self) -> List[Provider]:
        return [p for p in self._providers.values() if p.enabled]

    def list_by_category(self, category: str) -> List[Provider]:
        """List providers supporting a category (llm, image_gen, video_gen, etc.)."""
        return [p for p in self._providers.values()
                if p.enabled and category in p.categories]

    def list_api_providers(self) -> List[Provider]:
        """List raw API providers (not affiliate, not local)."""
        return [p for p in self._providers.values()
                if p.enabled and p.provider_type == PROVIDER_TYPE_API]

    def list_affiliate_providers(self) -> List[Provider]:
        return [p for p in self._providers.values()
                if p.enabled and p.provider_type == PROVIDER_TYPE_AFFILIATE]

    def find_cheapest(self, model_type: str, canonical_id: str = '') -> Optional[tuple]:
        """Find the cheapest provider for a model type (or specific canonical model).

        Returns (Provider, ProviderModel) or None.
        """
        best = None
        best_cost = float('inf')

        for p in self.list_api_providers():
            if not p.has_api_key():
                continue
            for pm in p.models.values():
                if pm.model_type != model_type or not pm.enabled:
                    continue
                if canonical_id and pm.canonical_id != canonical_id:
                    continue
                cost = pm.input_price + pm.output_price
                if cost < best_cost:
                    best_cost = cost
                    best = (p, pm)
        return best

    def find_fastest(self, model_type: str, canonical_id: str = '') -> Optional[tuple]:
        """Find the fastest provider (by avg_tok_per_s or avg_latency_ms)."""
        best = None
        best_speed = 0.0

        for p in self.list_api_providers():
            if not p.has_api_key():
                continue
            for pm in p.models.values():
                if pm.model_type != model_type or not pm.enabled:
                    continue
                if canonical_id and pm.canonical_id != canonical_id:
                    continue
                speed = pm.avg_tok_per_s if pm.avg_tok_per_s > 0 else (
                    1000.0 / pm.avg_latency_ms if pm.avg_latency_ms > 0 else 0.5)
                if speed > best_speed:
                    best_speed = speed
                    best = (p, pm)
        return best

    def find_best(self, model_type: str, canonical_id: str = '',
                  strategy: str = 'balanced') -> Optional[tuple]:
        """Find the best provider using a weighted strategy.

        Strategies:
          'cheapest' — minimize cost
          'fastest'  — maximize speed
          'quality'  — maximize quality score
          'balanced' — weighted: 40% quality, 30% speed, 20% reliability, 10% cost
        """
        if strategy == 'cheapest':
            return self.find_cheapest(model_type, canonical_id)
        if strategy == 'fastest':
            return self.find_fastest(model_type, canonical_id)

        candidates = []
        for p in self.list_api_providers():
            if not p.has_api_key():
                continue
            for pm in p.models.values():
                if pm.model_type != model_type or not pm.enabled:
                    continue
                if canonical_id and pm.canonical_id != canonical_id:
                    continue
                candidates.append((p, pm))

        if not candidates:
            return None

        def _score(pair):
            _, pm = pair
            cost = pm.input_price + pm.output_price
            # Normalize: lower cost = higher score (invert, cap at 10)
            cost_score = max(0, 1.0 - cost / 10.0)
            speed_score = min(1.0, pm.avg_tok_per_s / 200.0) if pm.avg_tok_per_s > 0 else 0.5

            if strategy == 'quality':
                return pm.quality_score
            # balanced
            return (0.40 * pm.quality_score +
                    0.30 * speed_score +
                    0.20 * pm.reliability +
                    0.10 * cost_score)

        candidates.sort(key=_score, reverse=True)
        return candidates[0]

    # ── Mutation ──────────────────────────────────────────────────────

    def register(self, provider: Provider, persist: bool = True):
        with self._lock:
            self._providers[provider.id] = provider
        if persist:
            self.save()

    def update_model_stats(self, provider_id: str, model_id: str,
                           tok_per_s: float = 0, latency_ms: float = 0,
                           quality: float = 0, success: bool = True):
        """Update efficiency stats for a provider model (called by gateway after each request)."""
        p = self._providers.get(provider_id)
        if not p:
            return
        pm = p.models.get(model_id)
        if not pm:
            return
        # Exponential moving average (alpha=0.1 for smooth updates)
        a = 0.1
        if tok_per_s > 0:
            pm.avg_tok_per_s = pm.avg_tok_per_s * (1 - a) + tok_per_s * a if pm.avg_tok_per_s > 0 else tok_per_s
        if latency_ms > 0:
            pm.avg_latency_ms = pm.avg_latency_ms * (1 - a) + latency_ms * a if pm.avg_latency_ms > 0 else latency_ms
        if quality > 0:
            pm.quality_score = pm.quality_score * (1 - a) + quality * a
        pm.reliability = pm.reliability * (1 - a) + (1.0 if success else 0.0) * a

    def update_health(self, provider_id: str, healthy: bool,
                      latency_ms: float = 0, error_rate: float = 0):
        p = self._providers.get(provider_id)
        if not p:
            return
        p.healthy = healthy
        p.last_health_check = time.time()
        p.avg_latency_ms = latency_ms
        p.error_rate_24h = error_rate

    def set_api_key(self, provider_id: str, api_key: str):
        """Set API key in environment for a provider."""
        p = self._providers.get(provider_id)
        if not p or not p.env_key:
            return False
        os.environ[p.env_key] = api_key
        p.api_key_set = True
        self.save()
        return True

    # ── Summary for agents ────────────────────────────────────────────

    def get_capabilities_summary(self) -> Dict[str, List[str]]:
        """Return {model_type: [provider_ids]} — what Nunba can do right now."""
        result: Dict[str, List[str]] = {}
        for p in self.list_enabled():
            for cat in p.categories:
                result.setdefault(cat, []).append(p.id)
        return result


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_registry: Optional[ProviderRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry()
    return _registry
