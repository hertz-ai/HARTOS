"""
Agent Lightning Integration Module

Integrates Microsoft Agent Lightning for continuous agent training and optimization.
Provides minimal-change wrappers and auto-tracing for existing AutoGen agents.
"""

__version__ = "1.0.0"

# Configuration
from .config import (
    AGENT_LIGHTNING_CONFIG,
    is_enabled,
    get_agent_config,
    get_reward_value
)

# Core components
from .wrapper import AgentLightningWrapper, instrument_autogen_agent
from .tracer import enable_auto_tracing, disable_auto_tracing, LightningTracer, Span
from .rewards import RewardCalculator, RewardType
from .store import LightningStore

__all__ = [
    # Config
    'AGENT_LIGHTNING_CONFIG',
    'is_enabled',
    'get_agent_config',
    'get_reward_value',

    # Core
    'AgentLightningWrapper',
    'instrument_autogen_agent',
    'enable_auto_tracing',
    'disable_auto_tracing',
    'LightningTracer',
    'Span',
    'RewardCalculator',
    'RewardType',
    'LightningStore',
]
