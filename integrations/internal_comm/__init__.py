"""Internal Agent Communication (In-Process Skill-Based Delegation)"""
from .internal_agent_communication import (
    AgentSkill, AgentSkillRegistry, A2AMessage, A2AContextExchange,
    skill_registry, a2a_context, register_agent_with_skills,
    create_delegation_function, create_context_sharing_function, create_context_retrieval_function
)

__all__ = [
    'AgentSkill', 'AgentSkillRegistry', 'A2AMessage', 'A2AContextExchange',
    'skill_registry', 'a2a_context', 'register_agent_with_skills',
    'create_delegation_function', 'create_context_sharing_function', 'create_context_retrieval_function'
]
