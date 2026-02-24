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
