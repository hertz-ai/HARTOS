"""
Tool Allowlist by Model Tier

Restricts which tools are available to each model tier.
FAST models get read-only tools, BALANCED gets read-write,
EXPERT gets unrestricted access.

Unknown models fail closed (no tools allowed).

Usage:
    from integrations.agent_engine.tool_allowlist import filter_tools_for_model, check_tool_allowed

    tools = filter_tools_for_model('groq-llama', all_tools)
    allowed, reason = check_tool_allowed('groq-llama', 'write_file')
"""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

# Lazy import to avoid circular dependencies
_ModelTier = None


def _get_model_tier():
    global _ModelTier
    if _ModelTier is None:
        from integrations.agent_engine.model_registry import ModelTier
        _ModelTier = ModelTier
    return _ModelTier


# Read-only tools safe for fast/cheap models
_FAST_TOOLS = frozenset({
    'web_search', 'read_file', 'list_files', 'memory_search',
    'embeddings_query', 'get_time', 'calculator', 'status_check',
    'get_weather', 'search_docs', 'get_agent_info',
})

# Read-write tools for balanced models
_BALANCED_TOOLS = _FAST_TOOLS | frozenset({
    'write_file', 'send_message', 'create_task', 'update_task',
    'post_content', 'schedule_job', 'send_notification',
})

# Expert = None (unrestricted)
_TIER_TOOLS = None  # Populated lazily


def _get_tier_tools() -> dict:
    """Lazy-init tier→tool mapping (avoids import-time ModelTier resolution)."""
    global _TIER_TOOLS
    if _TIER_TOOLS is not None:
        return _TIER_TOOLS

    ModelTier = _get_model_tier()
    _TIER_TOOLS = {
        ModelTier.FAST: _FAST_TOOLS,
        ModelTier.BALANCED: _BALANCED_TOOLS,
        ModelTier.EXPERT: None,  # None = unrestricted
    }
    return _TIER_TOOLS


def _resolve_tier(model_id: str):
    """Resolve model ID to its tier. Returns None if unknown."""
    try:
        from integrations.agent_engine.model_registry import model_registry
        info = model_registry.get(model_id)
        if info:
            return info.get('tier') or info.get('model_tier')
    except Exception:
        pass
    return None


def filter_tools_for_model(model_id: str, tools: List[dict]) -> List[dict]:
    """
    Filter a tool list by model tier.

    Args:
        model_id: Model identifier (e.g. 'groq-llama', 'gpt-4.1')
        tools: List of tool dicts (must have 'name' key)

    Returns:
        Filtered list. Expert tier returns all tools.
        Unknown model returns empty list (fail-closed).
    """
    tier = _resolve_tier(model_id)
    if tier is None:
        logger.warning(f"Tool allowlist: unknown model '{model_id}', fail-closed (no tools)")
        return []

    tier_tools = _get_tier_tools()
    allowed_set = tier_tools.get(tier)
    if allowed_set is None:
        return tools  # Expert = unrestricted

    filtered = [t for t in tools if t.get('name') in allowed_set]
    if len(filtered) < len(tools):
        blocked = [t.get('name') for t in tools if t.get('name') not in allowed_set]
        logger.info(f"Tool allowlist: {model_id} (tier={tier.value}) blocked tools: {blocked}")
    return filtered


def check_tool_allowed(model_id: str, tool_name: str) -> Tuple[bool, str]:
    """
    Gate function: check if a specific tool is allowed for a model.

    Returns:
        (allowed, reason)
    """
    tier = _resolve_tier(model_id)
    if tier is None:
        return False, f"Unknown model '{model_id}' — fail-closed"

    tier_tools = _get_tier_tools()
    allowed_set = tier_tools.get(tier)
    if allowed_set is None:
        return True, f"Model tier {tier.value} has unrestricted access"

    if tool_name in allowed_set:
        return True, f"Tool '{tool_name}' allowed for tier {tier.value}"

    return False, f"Tool '{tool_name}' not allowed for tier {tier.value}"


# ─── Capability summary for prompt injection ───────────────────────────
# Each static tool name → ≤3-word phrase the draft prompt can show the
# user-facing model.  Drift guard:
# tests/unit/test_draft_first_dispatch.py asserts every name in
# _FAST_TOOLS|_BALANCED_TOOLS has an entry here, so a new tool added
# without a description fails CI before shipping.
_TOOL_DESCRIPTIONS: dict = {
    # _FAST_TOOLS (read-only)
    'web_search':       'web search',
    'read_file':        'read files',
    'list_files':       'browse files',
    'memory_search':    'recall memory',
    'embeddings_query': 'semantic search',
    'get_time':         'current time',
    'calculator':       'math',
    'status_check':     'system status',
    'get_weather':      'weather',
    'search_docs':      'search docs',
    'get_agent_info':   'agent info',
    # _BALANCED_TOOLS adds (read-write)
    'write_file':         'write files',
    'send_message':       'send messages',
    'create_task':        'create tasks',
    'update_task':        'update tasks',
    'post_content':       'post content',
    'schedule_job':       'schedule jobs',
    'send_notification':  'notifications',
}


def get_capability_summary() -> str:
    """Comma-joined, ≤3-word capability list for the draft prompt.

    Combines:
      - Static tools (above), name → ≤3-word phrase
      - ModelCatalog entries, rolled up by type (tts/stt/vlm/video/audio)
        so 12 TTS voices show as one phrase, not 12 phrases
      - MCP servers, by server name (auto-discovered via mcp_registry)
      - Active channel adapters (auto-discovered via channels.admin.api)
      - Expert-agent registry, rolled up by category

    Every dynamic source is wrapped in try/except so a missing or not-
    yet-loaded subsystem silently drops its slice rather than blocking
    the prompt.  Result is intended to be ≤100 tokens at typical install.

    Single source of truth for "what can this assistant do" surfaced to
    the draft model — when a new MCP server / channel / video model is
    registered at runtime, it appears in the next call without code
    changes.
    """
    parts: list = []

    # Static tools (sorted for stable output)
    for name in sorted(_FAST_TOOLS | _BALANCED_TOOLS):
        parts.append(_TOOL_DESCRIPTIONS.get(name, name))

    # ModelCatalog — roll up by type, not per-entry
    try:
        from integrations.service_tools.model_catalog import get_catalog
        cat = get_catalog()
        type_counts: dict = {}
        for entry in cat.list_all():
            t = getattr(entry.model_type, 'value', str(entry.model_type))
            type_counts[t] = type_counts.get(t, 0) + 1
        _MODEL_TYPE_PHRASES = {
            'tts':       lambda n: f'TTS ({n} voices)',
            'stt':       lambda n: f'STT ({n} models)',
            'vlm':       lambda n: f'vision ({n} VLMs)',
            'video_gen': lambda _: 'video generation',
            'audio_gen': lambda _: 'audio generation',
            'llm':       lambda n: f'LLMs ({n})',
        }
        for t, n in type_counts.items():
            phrase_fn = _MODEL_TYPE_PHRASES.get(t)
            if phrase_fn:
                parts.append(phrase_fn(n))
    except Exception:
        pass

    # MCP servers (server names; tools-per-server would blow the budget)
    try:
        from integrations.mcp.mcp_integration import mcp_registry
        for server_name in mcp_registry.servers:
            parts.append(server_name)
    except Exception:
        pass

    # Channel adapters
    try:
        from integrations.channels.admin.api import api as channel_api
        for ch in channel_api._channels:
            parts.append(ch)
    except Exception:
        pass

    # Expert agents — category roll-up only (96 individual is too many)
    try:
        from integrations.expert_agents.registry import (
            ExpertAgentRegistry, AgentCategory,
        )
        registry = ExpertAgentRegistry()
        live_cats = [
            cat.value for cat in AgentCategory
            if registry.get_agents_by_category(cat)
        ]
        if live_cats:
            shown = ', '.join(live_cats[:5])
            ellipsis = '…' if len(live_cats) > 5 else ''
            parts.append(f'domain experts ({shown}{ellipsis})')
    except Exception:
        pass

    return ', '.join(parts)
