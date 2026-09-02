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
    DRAFT = "draft"        # Tiny (< 1B) models — first-responder / classifier
                           # only. Always local. Used by draft-first dispatcher
                           # to emit a standby reply + routing signal before
                           # waking a heavier model.
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

    def is_dispatchable(self) -> bool:
        """False for backends the router must not dial as a chat endpoint.

        'distributed-shard' advertises the WAN shard cluster with a NON-endpoint
        base_url ('shard://cluster'); it becomes selectable only once the
        shard-orchestrator lands to intercept it by model_id. Until then the
        selectors skip it so nothing dials the placeholder (the "Selection guard"
        promised in docs/architecture/SHARD_RUNTIME_HARTOS_SIDE.md). One check,
        used by every selector, keeps this a single source of truth.

        shard:// was the only scheme excluded, but it is not the only
        non-endpoint one. The registry also carries in-process media backends —
        'inprocess://pocket_tts', 'inprocess://whisper', 'inprocess://luxtts',
        'local://onnxruntime' — which are TTS/STT engines, not OpenAI-compatible
        chat servers. They were registered at FAST tier with real-looking
        accuracy scores and tiny latencies, so every latency-ordered selector
        picked them ahead of the actual language models.

        Measured on this desktop 2026-09-01, with six models registered:
          pocket-tts-100m    fast   inprocess://   200ms  acc 0.85
          qwen3.5-4b-local   fast   http://...            (the real fast model)
          claude-code        expert http://...
        get_fast_model() returned pocket-tts-100m — so speculative dispatch was
        asking a SPEECH SYNTHESISER to draft chat completions, and
        get_expert_model() on central returned whisper-stt-local for the same
        reason. An endpoint that cannot answer /chat/completions must never win
        a text-inference selection, whatever its latency.

        The rule is therefore about the transport, not a name list: a
        dispatchable backend is one the router can actually POST to.
        """
        url = str(self.config_list_entry.get('base_url', '') or '')
        return url.startswith('http://') or url.startswith('https://')

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

    def unregister(self, model_id: str) -> bool:
        """Remove a registered backend.

        Returns True if a backend was removed, False if no entry existed.
        Idempotent — callers that don't track whether a backend is
        currently registered (e.g. peer health-check loops) can call
        this safely on every disconnect.

        Used by HiveExpertDiscovery when a hive peer revokes its
        capability advertisement or fails health checks repeatedly.
        """
        with self._lock:
            existed = model_id in self._models
            if existed:
                del self._models[model_id]
        if existed:
            logger.info(f"ModelRegistry: unregistered {model_id}")
        return existed

    def get_model(self, model_id: str) -> Optional[ModelBackend]:
        with self._lock:
            return self._models.get(model_id)

    def get_draft_model(self) -> Optional[ModelBackend]:
        """Get the lowest-latency DRAFT tier model — the first-responder
        that drives `SpeculativeDispatcher.dispatch_draft_first()`. DRAFT
        models are tiny (<1B), always local, and are expected to emit a
        standby reply + routing signal before a heavier model is woken.

        When no DRAFT tier is registered, the MAIN LOCAL MODEL serves as the
        draft. A dedicated sub-1B draft only earns its VRAM on a box with
        headroom to host it alongside the main model; below that the same
        llama.cpp server (``core.port_registry.get_local_llm_url``) answers
        both roles, so draft-first still has a real first responder instead
        of bailing out with ``no_draft_model`` and silently disabling the
        selective-escalation path on every machine without a second model.

        Returns None only when this node has no dispatchable local model at
        all, which is the genuine "nothing can draft" case."""
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if m.tier == ModelTier.DRAFT
                # Same guard as the other selectors. This one omitted it, so a
                # non-endpoint backend at DRAFT tier would be handed to
                # dispatch_draft_first as the first responder.
                and m.is_dispatchable()
            ]
            if not candidates:
                # Fallback: the main local model doubles as the draft.
                # EXPERT is excluded deliberately — `claude-code` registers
                # is_local=True (it IS locally hosted), so a bare is_local
                # filter would hand the frontier/background tier to the hot
                # draft path and put a ~6s subscription call in front of
                # every turn. The draft must be the cheap local responder.
                candidates = [
                    m for m in self._models.values()
                    if m.is_local
                    and m.tier != ModelTier.EXPERT
                    and m.is_dispatchable()
                ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.avg_latency_ms)

    def get_fast_model(self, min_accuracy: float = 0.0) -> Optional[ModelBackend]:
        """Get the lowest-latency model meeting minimum accuracy.

        DRAFT tier is excluded from this selection — DRAFT models answer
        via the dedicated draft-first path, not the speculative fast
        path, because they can't produce final answers for complex tasks.
        """
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if m.accuracy_score >= min_accuracy
                and m.tier != ModelTier.DRAFT
                and m.is_dispatchable()
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.avg_latency_ms)

    def get_expert_model(self, max_cost: float = float('inf')) -> Optional[ModelBackend]:
        """Get the highest-accuracy model within budget. DRAFT models are
        excluded — they answer via the draft-first path, never as an
        expert cross-check."""
        with self._lock:
            candidates = [
                m for m in self._models.values()
                if m.cost_per_1k_tokens <= max_cost
                and m.tier != ModelTier.DRAFT
                and m.is_dispatchable()
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
                          if m.is_local and m.accuracy_score >= min_accuracy
                          # Same guard as the other selectors. Without it the
                          # in-process TTS/STT backends win here too: they are
                          # is_local=True with high accuracy scores, so
                          # 'local_only' and 'local_preferred' policies resolved
                          # to a speech engine instead of the local Qwen.
                          and m.is_dispatchable()]
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

    def speculation_pair(self, min_accuracy: float = 0.0):
        """The (fast, expert) pair speculation would use, or (None, None).

        Speculation needs two DIFFERENT dispatchable backends: a fast one to
        draft and an expert one to verify. That rule was written inline in
        SpeculativeDispatcher.should_speculate and then a second time in
        core.health_probe when it needed to report the same thing — two copies
        of one decision, which drift the moment either is touched.

        It belongs here, beside the selectors it composes, so the dispatcher
        and any probe ask the SAME question rather than each rebuilding it.
        """
        fast = self.get_fast_model(min_accuracy)
        expert = self.get_expert_model()
        if not fast or not expert or fast.model_id == expert.model_id:
            return None, None
        return fast, expert

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
    """Register default model backends.

    The text-LLM backends follow core.autogen_config, the ONE configured LLM:

      api    exactly one text backend, 'configured-api', the endpoint the
             user configured.  It serves every role: draft = fast = expert
             (owner design for a node with an API key and no VRAM: the single
             API is the only LLM it talks to).  No vendor entry registers
             beside it.  A second LLM picked from a stray env key
             (GROQ_API_KEY, DEEPSEEK_API_KEY, AZURE_OPENAI_API_KEY,
             ANTHROPIC_API_KEY, GLM_API_KEY, QWEN_API_KEY) was the side door
             that put central's goal turns on a model nobody configured
             (#69), so those blocks are gone: an API is configured through
             HEVOLVE_LLM_ENDPOINT_URL / HEVOLVE_LLM_MODEL_NAME /
             HEVOLVE_LLM_API_KEY (Nunba's key vault exports exactly that) and
             nowhere else.
      local  the node's own llama-server(s): the 0.8B draft when a draft
             server listens, and the 4B main model.

    claude-code (the copilot's subscription, the frontier tier) and the
    non-LLM media backends register in both kinds.
    """
    from core.autogen_config import resolve_llm_backend, OPENAI_SDK_DEFAULT_BASE
    _kind, _entry = resolve_llm_backend()

    if _kind == 'api':
        from integrations.agent_engine.budget_gate import spark_per_1k
        _cfg = dict(_entry)
        # autogen's client defaults to the openai base when the entry has no
        # base_url; the selectors need the URL spelled out to know the
        # backend is dispatchable, and llm_http_target posts there too.
        _cfg.setdefault('base_url', OPENAI_SDK_DEFAULT_BASE)
        model_registry.register(ModelBackend(
            model_id='configured-api',
            display_name='Configured API (%s)' % (_cfg.get('model') or 'no model name'),
            tier=ModelTier.FAST,
            config_list_entry=_cfg,
            avg_latency_ms=1500.0,     # prior; record_latency moves it
            accuracy_score=0.85,       # prior; update_accuracy moves it
            cost_per_1k_tokens=spark_per_1k(_cfg.get('model', '')),
            # This node's own LLM: dispatch runs the full /chat pipeline
            # (agents, tools), not the hive-peer POST that is_local=False
            # selects in SpeculativeDispatcher._dispatch_expert_langchain.
            is_local=True,
            hardware_dependent=False,
            gpu_tdp_watts=0.0,         # not this node's silicon
        ))

    if _kind == 'local':
        # 0. Local Qwen3.5 0.8B — DRAFT tier first-responder for the
        #    draft-first dispatcher. Its job is to emit an immediate standby
        #    reply plus a JSON routing signal saying whether the request can
        #    be handled locally or needs delegation to the 4B or the hive.
        #    ~200-400ms on consumer GPUs. Its interactions flow through
        #    WorldModelBridge.record_interaction so HevolveAI's continual
        #    learner can distill expert→draft over time (the draft gets
        #    better at knowing when to delegate vs answer directly).
        #
        #    The draft runs on port 8081 (vlm_caption) — NOT the main LLM
        #    port 8080 where the 4B lives. Both servers stay resident so
        #    draft-first pays real draft latency (~300ms) and normal chat
        #    pays 4B latency (~700ms) without swapping. Nunba's
        #    LlamaConfig.start_caption_server owns the 0.8B process and
        #    main.py kicks it off during _deferred_platform_init.
        from core.port_registry import get_local_llm_url, get_local_draft_url
        _local_url = get_local_llm_url()
        _draft_url = get_local_draft_url()

        model_registry.register(ModelBackend(
            model_id='qwen3.5-0.8b-draft',
            display_name='Qwen3.5 0.8B (Draft)',
            tier=ModelTier.DRAFT,
            config_list_entry={
                'model': 'Qwen3.5-0.8B-Instruct',
                'api_key': 'dummy',
                'base_url': _draft_url,
                'price': [0, 0],
            },
            avg_latency_ms=300.0,
            accuracy_score=0.45,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
            gpu_tdp_watts=80.0,
        ))

        # 1. Local Qwen3.5-4B VL — default local model (256K context, vision+text, llama.cpp b8148+)

        model_registry.register(ModelBackend(
            model_id='qwen3.5-4b-local',
            display_name='Qwen3.5 4B VL (Local)',
            tier=ModelTier.FAST,
            config_list_entry={
                'model': 'Qwen3.5-4B',
                'api_key': 'dummy',
                'base_url': _local_url,
                'price': [0, 0],
            },
            avg_latency_ms=700.0,
            accuracy_score=0.60,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
            gpu_tdp_watts=170.0,
        ))

        # 1b. Local Qwen3-VL-4B — fallback for older llama.cpp installs
        model_registry.register(ModelBackend(
            model_id='qwen3-vl-4b-local',
            display_name='Qwen3-VL 4B (Local)',
            tier=ModelTier.FAST,
            config_list_entry={
                'model': 'Qwen3-VL-4B-Instruct',
                'api_key': 'dummy',
                'base_url': _local_url,
                'price': [0, 0],
            },
            avg_latency_ms=800.0,
            accuracy_score=0.55,
            cost_per_1k_tokens=0.0,
            is_local=True,
            hardware_dependent=True,
            gpu_tdp_watts=170.0,
        ))

    # 5a2. Claude Code (the SUBSCRIPTION path — NOT the API key above). The
    #      resident, already-authorized `claude -p` served as an
    #      OpenAI-compatible endpoint by integrations.providers
    #      .claude_code_endpoint (/api/claude/v1). This is how HARTOS "wears
    #      Claude as its engine" using the ONE credential the copilot logged in
    #      with — no ANTHROPIC_API_KEY, no per-token cost. It is a LOCAL expert
    #      (is_local=True) in the same registry the hive uses for remote experts.
    #
    #      Gated on claude_code_available(): a node without Claude Code logged in
    #      simply lacks THIS frontier tier and falls back to the hive experts /
    #      local models — it never registers a backend that 503s every call.
    #      Higher latency + lower nominal accuracy-vs-cost than the API path is
    #      deliberate: `claude -p` is the agent binary in print mode, so it is
    #      the frontier/background tier, never the hot draft path.
    try:
        from integrations.coding_agent.claude_code_backend import claude_code_available
        if claude_code_available():
            from core.port_registry import get_port
            _cc_base = 'http://127.0.0.1:%s/api/claude/v1' % get_port('backend')
            model_registry.register(ModelBackend(
                model_id='claude-code',
                display_name='Claude Code (Frontier, subscription)',
                tier=ModelTier.EXPERT,
                config_list_entry={
                    'model': 'claude-code',
                    'api_key': 'dummy',            # local shim; auth is the sub
                    'base_url': _cc_base,
                    'price': [0, 0],               # subscription, not per-token
                },
                avg_latency_ms=6000.0,             # agent binary in print mode
                accuracy_score=0.95,
                cost_per_1k_tokens=0.0,
                is_local=True,
                hardware_dependent=False,
            ))
    except Exception as _cc_err:                    # never break registry init
        import logging as _l
        _l.getLogger(__name__).debug("claude-code backend not registered: %s", _cc_err)

    # 5c. Distributed shard cluster (WAN pipeline-parallel inference) — feature-flagged.
    #     A model too big for one node is served by K peers, each holding a
    #     contiguous LAYER range; only the hidden-state activation crosses the
    #     wire (never weights). HARTOS owns the mesh/relay/trust/economics; the
    #     per-shard forward pass lives behind hevolveai's Model Bus /v1/shard/*
    #     verbs (frozen boundary: hevolveai/docs/SHARD_RUNTIME_CONTRACT.md,
    #     mirrored in docs/architecture/SHARD_RUNTIME_HARTOS_SIDE.md).
    #
    #     This entry only ADVERTISES the capability so routing can see it. Actual
    #     dispatch is intercepted by the shard-orchestrator on model_id match
    #     BEFORE any OpenAI-style client is built (the 'shard://cluster' base_url
    #     is a deliberate non-endpoint — it must never be dialled directly). Off
    #     by default; set HART_SHARD_RUNTIME=1 on a node that has joined a shard
    #     cluster. Until the orchestrator lands, the dispatcher must skip this
    #     model_id (see the side doc §"Selection guard").
    if os.environ.get('HART_SHARD_RUNTIME', '').lower() in ('1', 'true', 'yes'):
        model_registry.register(ModelBackend(
            model_id='distributed-shard',
            display_name='Distributed Shard Cluster (WAN pipeline)',
            tier=ModelTier.EXPERT,
            config_list_entry={
                'model': os.environ.get('HART_SHARD_MODEL', 'distributed-shard'),
                'api_key': 'local',
                'base_url': 'shard://cluster',  # non-endpoint: orchestrator intercepts by model_id
                'price': [0, 0],
            },
            avg_latency_ms=6000.0,   # WAN pipeline hops — slow but unlocks models no single node can host
            accuracy_score=0.92,     # runs the full big model, so quality tracks the model, not the split
            cost_per_1k_tokens=0.0,  # peer compute, not a paid API
            is_local=False,          # distributed across peers, not this box
            hardware_dependent=True,
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
