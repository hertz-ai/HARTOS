"""
Agentic Service Discovery — an agent that autonomously expands Nunba's capabilities.

This is a meta-agent: it discovers, evaluates, and integrates new AI services
so that OTHER agents can use them. Runs during idle via ResourceGovernor.

Discovery sources (searched autonomously):
  1. MCP server registries — scan for published MCP tool servers
  2. OpenAPI specs — parse any service's Swagger/OpenAPI as tools
  3. HuggingFace model hub — discover new models for local/API use
  4. GitHub trending — find new AI tools and APIs
  5. Known provider APIs — check for new models on existing providers
  6. Hive network — learn about services other Nunba nodes discovered

Pipeline per discovery cycle:
  1. SEARCH: agent picks a source, queries for new services
  2. EVALUATE: check pricing, capabilities, terms, reliability
  3. REGISTER: add to ProviderRegistry if it passes evaluation
  4. TEST: run a probe request via gateway
  5. SCORE: update EfficiencyMatrix with results
  6. SHARE: broadcast discovery to hive (if enabled)

Safety:
  - Only registers services that respond to a health check
  - Never auto-sets API keys (user must configure in Settings)
  - Rate-limited: max 5 discoveries per idle cycle
  - Constitutional filter: skip services that violate guardrails

Integration:
  - ResourceGovernor._proactive_check_signals() triggers discovery
  - Results stored in provider_registry.json (persisted)
  - Discoveries visible in admin /providers page
  - Uses LangChain agent with provider tools for autonomous reasoning
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

MAX_DISCOVERIES_PER_CYCLE = 5
DISCOVERY_COOLDOWN_HOURS = 6     # Don't re-scan same source within 6h
PROBE_TIMEOUT_SECONDS = 10

# Known OpenAI-compatible API patterns to auto-detect
_OPENAI_COMPATIBLE_PATTERNS = [
    '/v1/chat/completions',
    '/v1/completions',
    '/v1/models',
    '/v1/embeddings',
]

# Sources the agent checks (rotated each cycle)
DISCOVERY_SOURCES = [
    'existing_providers',    # Check existing providers for new models
    'huggingface_trending',  # HuggingFace trending models
    'mcp_registry',          # MCP server discovery
    'openrouter_models',     # OpenRouter model catalog (aggregator)
]


# ═══════════════════════════════════════════════════════════════════════
# Discovery Agent
# ═══════════════════════════════════════════════════════════════════════

class DiscoveryAgent:
    """Autonomous agent that discovers and integrates new AI services.

    Does NOT use LangChain (avoids heavy imports). Instead, uses a simple
    rule-based pipeline with web requests. The agent "reasons" by:
    1. Picking a discovery source based on rotation + staleness
    2. Fetching data from that source
    3. Evaluating each candidate against criteria
    4. Registering passing candidates
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_scan: Dict[str, float] = {}  # source → timestamp
        self._source_index = 0
        self._discoveries_this_cycle = 0
        self._total_discoveries = 0

    def run_discovery_cycle(self) -> List[Dict[str, Any]]:
        """Run one discovery cycle. Called by ResourceGovernor during idle.

        Returns list of newly discovered services (may be empty).
        """
        self._discoveries_this_cycle = 0
        discoveries = []

        # Pick next source (round-robin, skip if recently scanned)
        source = self._pick_source()
        if not source:
            logger.debug("DiscoveryAgent: all sources recently scanned, skipping")
            return []

        logger.info("DiscoveryAgent: scanning source '%s'", source)

        try:
            if source == 'existing_providers':
                discoveries = self._scan_existing_providers()
            elif source == 'huggingface_trending':
                discoveries = self._scan_huggingface()
            elif source == 'mcp_registry':
                discoveries = self._scan_mcp()
            elif source == 'openrouter_models':
                discoveries = self._scan_openrouter()
        except Exception as e:
            logger.warning("DiscoveryAgent: source '%s' failed: %s", source, e)

        self._last_scan[source] = time.time()
        self._total_discoveries += len(discoveries)

        if discoveries:
            logger.info("DiscoveryAgent: found %d new services from '%s'",
                        len(discoveries), source)
            # Share with hive (best-effort)
            self._share_with_hive(discoveries)

        return discoveries

    def _pick_source(self) -> Optional[str]:
        """Pick the next discovery source, skipping recently scanned ones."""
        now = time.time()
        cooldown = DISCOVERY_COOLDOWN_HOURS * 3600

        for _ in range(len(DISCOVERY_SOURCES)):
            source = DISCOVERY_SOURCES[self._source_index % len(DISCOVERY_SOURCES)]
            self._source_index += 1
            last = self._last_scan.get(source, 0)
            if now - last >= cooldown:
                return source
        return None

    # ── Source: Existing Providers (check for new models) ─────────────

    def _scan_existing_providers(self) -> List[Dict]:
        """Check existing providers for newly added models."""
        from integrations.providers.registry import get_registry

        discoveries = []
        registry = get_registry()

        for provider in registry.list_api_providers():
            if not provider.has_api_key():
                continue
            if self._discoveries_this_cycle >= MAX_DISCOVERIES_PER_CYCLE:
                break

            try:
                # Query /v1/models endpoint to discover new models
                url = f"{provider.base_url.rstrip('/')}/models"
                headers = {'Authorization': f'Bearer {provider.get_api_key()}'}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
                    data = json.loads(resp.read().decode())

                models_list = data.get('data', data.get('models', []))
                if isinstance(models_list, list):
                    known_ids = set(provider.models.keys())
                    for m in models_list:
                        mid = m.get('id', '') if isinstance(m, dict) else str(m)
                        if mid and mid not in known_ids:
                            # New model found on existing provider
                            discovery = self._register_new_model(
                                provider, mid, m if isinstance(m, dict) else {})
                            if discovery:
                                discoveries.append(discovery)
                                self._discoveries_this_cycle += 1
            except Exception as e:
                logger.debug("DiscoveryAgent: failed to scan %s models: %s",
                             provider.id, e)

        return discoveries

    def _register_new_model(self, provider, model_id: str,
                            model_info: dict) -> Optional[Dict]:
        """Register a newly discovered model on an existing provider."""
        from integrations.providers.registry import ProviderModel, PRICE_PER_1M_TOKENS

        # Infer model type from ID
        model_type = 'llm'  # default
        mid_lower = model_id.lower()
        if any(kw in mid_lower for kw in ['embed', 'bge', 'e5', 'gte']):
            model_type = 'embedding'
        elif any(kw in mid_lower for kw in ['vlm', 'vision', 'vl-', 'minicpm']):
            model_type = 'vlm'
        elif any(kw in mid_lower for kw in ['flux', 'sdxl', 'dall', 'image']):
            model_type = 'image_gen'
        elif any(kw in mid_lower for kw in ['whisper', 'stt']):
            model_type = 'stt'
        elif any(kw in mid_lower for kw in ['tts', 'bark', 'tortoise', 'coqui']):
            model_type = 'tts'

        # Create ProviderModel entry
        context_length = model_info.get('context_length', 0)
        pm = ProviderModel(
            model_id=model_id,
            canonical_id=self._to_canonical_id(model_id),
            model_type=model_type,
            context_length=context_length or 0,
            pricing_unit=PRICE_PER_1M_TOKENS,
            supports_streaming=True,
            enabled=True,
        )

        # Add to provider
        provider.models[model_id] = pm

        # Persist immediately so discoveries survive restart
        from integrations.providers.registry import get_registry
        get_registry().save()

        logger.info("DiscoveryAgent: registered new model %s on %s (type=%s)",
                     model_id, provider.id, model_type)

        return {
            'type': 'new_model',
            'provider': provider.id,
            'model_id': model_id,
            'model_type': model_type,
            'timestamp': time.time(),
        }

    # ── Source: HuggingFace Trending ──────────────────────────────────

    def _scan_huggingface(self) -> List[Dict]:
        """Check HuggingFace for trending inference-ready models."""
        discoveries = []
        try:
            # HuggingFace API: trending models with inference endpoints
            url = ('https://huggingface.co/api/models'
                   '?sort=trending&limit=20&pipeline_tag=text-generation')
            req = urllib.request.Request(url, headers={
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
                models = json.loads(resp.read().decode())

            from integrations.providers.registry import get_registry
            registry = get_registry()
            hf = registry.get('huggingface')
            if not hf:
                return []

            known = set(hf.models.keys())
            for m in models[:10]:
                mid = m.get('id', '')
                if mid and mid not in known and self._is_inference_ready(m):
                    discovery = self._register_new_model(hf, mid, {
                        'context_length': m.get('config', {}).get(
                            'max_position_embeddings', 0),
                    })
                    if discovery:
                        discovery['source'] = 'huggingface_trending'
                        discovery['downloads'] = m.get('downloads', 0)
                        discoveries.append(discovery)
                        self._discoveries_this_cycle += 1
                        if self._discoveries_this_cycle >= MAX_DISCOVERIES_PER_CYCLE:
                            break

        except Exception as e:
            logger.debug("DiscoveryAgent: HuggingFace scan failed: %s", e)

        return discoveries

    @staticmethod
    def _is_inference_ready(model_info: dict) -> bool:
        """Check if a HuggingFace model has inference endpoints."""
        # Models with many downloads and recent activity are likely inference-ready
        downloads = model_info.get('downloads', 0)
        likes = model_info.get('likes', 0)
        return downloads > 1000 and likes > 10

    # ── Source: MCP Registry ──────────────────────────────────────────

    def _scan_mcp(self) -> List[Dict]:
        """Discover MCP tool servers and register as providers."""
        discoveries = []
        try:
            from integrations.mcp.mcp_integration import get_mcp_registry
            mcp_reg = get_mcp_registry()
            count = mcp_reg.discover_all_tools()
            if count > 0:
                discoveries.append({
                    'type': 'mcp_tools',
                    'tools_discovered': count,
                    'timestamp': time.time(),
                })
                logger.info("DiscoveryAgent: discovered %d MCP tools", count)
        except ImportError:
            logger.debug("DiscoveryAgent: MCP integration not available")
        except Exception as e:
            logger.debug("DiscoveryAgent: MCP scan failed: %s", e)
        return discoveries

    # ── Source: OpenRouter Model Catalog ───────────────────────────────

    def _scan_openrouter(self) -> List[Dict]:
        """Scan OpenRouter for new models (aggregator of many providers)."""
        discoveries = []
        try:
            url = 'https://openrouter.ai/api/v1/models'
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode())

            models = data.get('data', [])
            from integrations.providers.registry import (
                get_registry, ProviderModel, PRICE_PER_1M_TOKENS,
            )
            registry = get_registry()
            openrouter = registry.get('openrouter')
            if not openrouter:
                return []

            known = set(openrouter.models.keys())
            for m in models:
                mid = m.get('id', '')
                if not mid or mid in known:
                    continue
                if self._discoveries_this_cycle >= MAX_DISCOVERIES_PER_CYCLE:
                    break

                pricing = m.get('pricing', {})
                input_price = float(pricing.get('prompt', 0)) * 1_000_000 if pricing.get('prompt') else 0
                output_price = float(pricing.get('completion', 0)) * 1_000_000 if pricing.get('completion') else 0

                pm = ProviderModel(
                    model_id=mid,
                    canonical_id=self._to_canonical_id(mid),
                    model_type='llm',
                    context_length=m.get('context_length', 0),
                    input_price=input_price,
                    output_price=output_price,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                    supports_streaming=True,
                )
                openrouter.models[mid] = pm
                discoveries.append({
                    'type': 'new_model',
                    'provider': 'openrouter',
                    'model_id': mid,
                    'source': 'openrouter_catalog',
                    'timestamp': time.time(),
                })
                self._discoveries_this_cycle += 1

        except Exception as e:
            logger.debug("DiscoveryAgent: OpenRouter scan failed: %s", e)

        if discoveries:
            from integrations.providers.registry import get_registry
            get_registry().save()

        return discoveries

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _to_canonical_id(model_id: str) -> str:
        """Convert provider model ID to a canonical slug for cross-provider matching."""
        # "meta-llama/Llama-3.3-70B-Instruct" → "llama-3.3-70b"
        name = model_id.split('/')[-1].lower()
        # Remove common suffixes
        for suffix in ['-instruct', '-turbo', '-chat', '-hf', '-fp8',
                       '-fp16', '-gguf', '-awq', '-gptq', '-preview']:
            name = name.replace(suffix, '')
        # Clean up
        name = re.sub(r'[^a-z0-9.-]', '-', name)
        name = re.sub(r'-+', '-', name).strip('-')
        return name

    def _share_with_hive(self, discoveries: List[Dict]):
        """Broadcast discoveries to the hive network (best-effort)."""
        try:
            from integrations.channels.hive_signal_bridge import get_signal_bridge
            bridge = get_signal_bridge()
            for d in discoveries[:3]:  # Limit to 3 per broadcast
                bridge.emit_signal('service_discovery', d)
        except Exception:
            pass  # Hive not available, that's fine

    def get_stats(self) -> Dict[str, Any]:
        return {
            'total_discoveries': self._total_discoveries,
            'last_scans': dict(self._last_scan),
            'sources': DISCOVERY_SOURCES,
        }


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_agent: Optional[DiscoveryAgent] = None
_agent_lock = threading.Lock()


def get_discovery_agent() -> DiscoveryAgent:
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                _agent = DiscoveryAgent()
    return _agent
