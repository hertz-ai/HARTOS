"""
Agent Lightning Configuration

Central configuration for Agent Lightning integration with our agent system.
"""

import os
from typing import Dict, Any, Optional

# Agent Lightning feature flag
AGENT_LIGHTNING_ENABLED = os.getenv('AGENT_LIGHTNING_ENABLED', 'false').lower() == 'true'

# Configuration dictionary
AGENT_LIGHTNING_CONFIG: Dict[str, Any] = {
    # Global settings
    'enabled': AGENT_LIGHTNING_ENABLED,
    'auto_trace': True,  # Enable automatic tracing
    'store_backend': os.getenv('AGENT_LIGHTNING_STORE', 'json'),  # redis, json, or memory

    # Storage paths
    'store_path': os.getenv('AGENT_LIGHTNING_STORE_PATH', './agent_data/lightning_store'),
    'traces_path': os.getenv('AGENT_LIGHTNING_TRACES_PATH', './agent_data/lightning_traces'),

    # Training configuration
    'training': {
        'enabled': True,
        'algorithm': 'prompt_opt',  # ppo, prompt_opt, sft
        'batch_size': 32,
        'learning_rate': 1e-4,
        'update_frequency': '1 hour',  # How often to retrain
        'min_samples': 100,  # Minimum samples before training
    },

    # Reward configuration
    'rewards': {
        'task_completion': 1.0,
        'task_failure': -0.5,
        'tool_use_efficiency': 0.1,
        'response_quality': 0.3,
        'execution_time': -0.1,  # Negative reward for slow execution
        'user_feedback': 0.5,
    },

    # Agent-specific configuration
    'agents': {
        'create_recipe_assistant': {
            'optimize_prompts': True,
            'optimize_tools': False,
            'track_tool_usage': True,
            'collect_feedback': True,
        },
        'reuse_recipe_assistant': {
            'optimize_prompts': True,
            'optimize_tools': True,
            'track_tool_usage': True,
            'collect_feedback': True,
        },
        'default': {
            'optimize_prompts': True,
            'optimize_tools': False,
            'track_tool_usage': True,
            'collect_feedback': False,
        }
    },

    # Monitoring and logging
    'monitoring': {
        'enabled': True,
        'log_level': 'INFO',
        'metrics_interval': 60,  # seconds
        'save_traces': True,
    },

    # Performance settings
    'performance': {
        'async_emit': True,  # Emit events asynchronously
        'batch_emit': True,  # Batch events before emitting
        'batch_size': 10,
        'emit_timeout': 5,  # seconds
    },

    # Integration settings
    'integration': {
        'autogen_compatible': True,
        'task_ledger_integration': True,
        'a2a_integration': True,
        'ap2_integration': True,
    }
}


def is_enabled() -> bool:
    """Check if Agent Lightning is enabled"""
    return AGENT_LIGHTNING_CONFIG['enabled']


def get_agent_config(agent_id: str) -> Dict[str, Any]:
    """
    Get configuration for a specific agent

    Args:
        agent_id: Agent identifier

    Returns:
        Agent-specific configuration
    """
    agents_config = AGENT_LIGHTNING_CONFIG.get('agents', {})

    # Try exact match first
    if agent_id in agents_config:
        return agents_config[agent_id]

    # Check for partial match (e.g., "reuse_8888_assistant" matches "reuse_recipe_assistant")
    for agent_pattern, config in agents_config.items():
        if agent_pattern in agent_id or agent_id.startswith(agent_pattern.split('_')[0]):
            return config

    # Return default configuration
    return agents_config.get('default', {
        'optimize_prompts': True,
        'optimize_tools': False,
        'track_tool_usage': True,
        'collect_feedback': False,
    })


def get_reward_value(reward_type: str) -> float:
    """
    Get reward value for a specific reward type

    Args:
        reward_type: Type of reward

    Returns:
        Reward value
    """
    rewards = AGENT_LIGHTNING_CONFIG.get('rewards', {})
    return rewards.get(reward_type, 0.0)


def get_store_backend() -> str:
    """Get the configured store backend"""
    return AGENT_LIGHTNING_CONFIG.get('store_backend', 'json')


def get_training_config() -> Dict[str, Any]:
    """Get training configuration"""
    return AGENT_LIGHTNING_CONFIG.get('training', {})


def update_config(updates: Dict[str, Any]) -> None:
    """
    Update configuration dynamically

    Args:
        updates: Dictionary of configuration updates
    """
    def deep_update(d: Dict, u: Dict) -> Dict:
        """Recursively update nested dictionaries"""
        for k, v in u.items():
            if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                d[k] = deep_update(d[k], v)
            else:
                d[k] = v
        return d

    deep_update(AGENT_LIGHTNING_CONFIG, updates)


# Environment-based overrides
if os.getenv('AGENT_LIGHTNING_DEBUG', 'false').lower() == 'true':
    AGENT_LIGHTNING_CONFIG['monitoring']['log_level'] = 'DEBUG'
    AGENT_LIGHTNING_CONFIG['monitoring']['save_traces'] = True


__all__ = [
    'AGENT_LIGHTNING_CONFIG',
    'AGENT_LIGHTNING_ENABLED',
    'is_enabled',
    'get_agent_config',
    'get_reward_value',
    'get_store_backend',
    'get_training_config',
    'update_config',
]
