"""Google A2A (Agent2Agent) Protocol Integration - Dynamic Agent System"""
from .google_a2a_integration import (
    TaskState, AgentCard, A2ATask, A2AMessageHandler, A2AProtocolServer,
    initialize_a2a_server, get_a2a_server, A2A_PROTOCOL_VERSION
)
from .dynamic_agent_registry import (
    DynamicAgentDiscovery, DynamicAgentExecutor, TrainedAgent,
    get_dynamic_discovery, get_dynamic_executor
)
from .register_dynamic_agents import (
    register_all_dynamic_agents, get_registered_agent_info, list_available_agents
)

# Keep old imports for backward compatibility but mark as deprecated
from .a2a_agent_registry import (
    register_all_agents as register_all_agents_legacy,
    assistant_executor, helper_executor, executor_executor, verify_executor,
    ASSISTANT_SKILLS, HELPER_SKILLS, EXECUTOR_SKILLS, VERIFY_SKILLS
)

# Primary API uses dynamic agents
register_all_agents = register_all_dynamic_agents

__all__ = [
    # Core A2A Protocol
    'TaskState', 'AgentCard', 'A2ATask', 'A2AMessageHandler', 'A2AProtocolServer',
    'initialize_a2a_server', 'get_a2a_server', 'A2A_PROTOCOL_VERSION',

    # Dynamic Agent System (NEW - Primary API)
    'register_all_agents', 'register_all_dynamic_agents',
    'DynamicAgentDiscovery', 'DynamicAgentExecutor', 'TrainedAgent',
    'get_dynamic_discovery', 'get_dynamic_executor',
    'get_registered_agent_info', 'list_available_agents',

    # Legacy Hardcoded Agents (DEPRECATED - for backward compatibility)
    'register_all_agents_legacy',
    'assistant_executor', 'helper_executor', 'executor_executor', 'verify_executor',
    'ASSISTANT_SKILLS', 'HELPER_SKILLS', 'EXECUTOR_SKILLS', 'VERIFY_SKILLS'
]
