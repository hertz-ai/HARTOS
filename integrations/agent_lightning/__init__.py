"""
Agent Lightning Integration Module

Integrates Microsoft Agent Lightning for continuous agent training and optimization.
Provides minimal-change wrappers and auto-tracing for existing AutoGen agents.
"""

__version__ = "1.0.0"

RECIPE_ASSISTANT_FLOWS = ('create', 'reuse')


def recipe_assistant_agent_id(flow: str, session_key: str) -> str:
    """Canonical Lightning id used by instrumentation and baseline readers."""
    flow = str(flow).strip().lower()
    if flow not in RECIPE_ASSISTANT_FLOWS:
        raise ValueError(f'Unsupported recipe assistant flow: {flow!r}')
    return f'{flow}_recipe_assistant_{session_key}'


def recipe_assistant_agent_ids(session_key: str):
    """Every recipe participant currently instrumented for this session."""
    return [recipe_assistant_agent_id(flow, session_key)
            for flow in RECIPE_ASSISTANT_FLOWS]

# Configuration
from .config import (
    AGENT_LIGHTNING_CONFIG,
    is_enabled,
    get_agent_config,
    get_reward_value
)

# Core components
from .wrapper import (
    AgentLightningWrapper, instrument_autogen_agent, record_verified_outcome,
    record_verified_outcome_for_agents,
)
from .tracer import enable_auto_tracing, disable_auto_tracing, LightningTracer, Span
from .rewards import RewardCalculator, RewardType
from .store import LightningStore

__all__ = [
    # Config
    'AGENT_LIGHTNING_CONFIG',
    'is_enabled',
    'get_agent_config',
    'get_reward_value',
    'RECIPE_ASSISTANT_FLOWS',
    'recipe_assistant_agent_id',
    'recipe_assistant_agent_ids',

    # Core
    'AgentLightningWrapper',
    'instrument_autogen_agent',
    'record_verified_outcome',
    'record_verified_outcome_for_agents',
    'enable_auto_tracing',
    'disable_auto_tracing',
    'LightningTracer',
    'Span',
    'RewardCalculator',
    'RewardType',
    'LightningStore',
]
