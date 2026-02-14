"""
Expert Agents Integration - Dream Fulfillment Network for Autogen

This module integrates the 96-agent expert network with the Autogen framework.
It bridges expert agents with AgentSkillRegistry, SmartLedger, and human-in-the-loop.

Usage:
    from integrations.expert_agents import get_expert_for_task, register_all_experts

    # Register all 96 expert agents with the skill registry
    register_all_experts(skill_registry)

    # Get best expert for a task (with human confirmation)
    expert_agent = get_expert_for_task("I want to build a mobile app", skill_registry)
"""

from typing import Dict, List, Optional, Any
from .registry import ExpertAgentRegistry, ExpertAgent, AgentCategory
import logging

logger = logging.getLogger(__name__)


def register_all_experts(skill_registry) -> Dict[str, ExpertAgent]:
    """
    Register all 96 expert agents with the AgentSkillRegistry.

    This makes expert agents discoverable via the existing skill registry system.

    Args:
        skill_registry: Instance of AgentSkillRegistry from internal_comm

    Returns:
        Dictionary of expert_id -> ExpertAgent
    """
    logger.info("Registering 96 expert agents with skill registry...")

    # Load expert registry
    expert_registry = ExpertAgentRegistry()

    # Register each expert with the skill registry
    for agent_id, expert in expert_registry.agents.items():
        # Convert expert capabilities to skill registry format
        skills = []
        for capability in expert.capabilities:
            skills.append({
                'name': capability.name,
                'description': capability.description,
                'proficiency': expert.reliability,  # Use expert reliability as proficiency
                'metadata': {
                    'example_use': capability.example_use,
                    'category': expert.category.value,
                    'model_type': expert.model_type,
                    'endpoint': expert.endpoint,
                    'cost_per_call': expert.cost_per_call,
                    'avg_latency_ms': expert.avg_latency_ms
                }
            })

        # Register with skill registry
        skill_registry.register_agent(agent_id, skills)

    logger.info(f"Successfully registered {len(expert_registry.agents)} expert agents")
    return expert_registry.agents


def get_expert_for_task(task_description: str, skill_registry,
                        category: Optional[AgentCategory] = None,
                        require_human_approval: bool = True) -> Optional[str]:
    """
    Find the best expert agent for a given task with human-in-the-loop.

    This is the key function for dream fulfillment - it intelligently matches
    tasks to the best expert agent and allows human oversight.

    Args:
        task_description: What the user wants to achieve
        skill_registry: Instance of AgentSkillRegistry
        category: Optional category filter
        require_human_approval: If True, ask human to confirm expert selection

    Returns:
        agent_id of selected expert, or None if no match
    """
    logger.info(f"Finding expert for task: {task_description}")

    # Load expert registry
    expert_registry = ExpertAgentRegistry()

    # Search for matching experts
    if category:
        # Filter by category first
        candidates = expert_registry.get_agents_by_category(category)
        # Then search within category
        matched = expert_registry.search_agents(task_description, category.value)
    else:
        # Search across all experts
        matched = expert_registry.search_agents(task_description)

    if not matched:
        logger.warning(f"No expert found for task: {task_description}")
        return None

    # Get top 3 candidates
    top_candidates = matched[:3]

    if require_human_approval:
        # Present options to human
        print("\n" + "="*80)
        print(f"TASK: {task_description}")
        print("="*80)
        print("\nRecommended Expert Agents:\n")

        for i, expert in enumerate(top_candidates, 1):
            print(f"{i}. {expert.name} ({expert.category.value})")
            print(f"   Description: {expert.description}")
            print(f"   Capabilities: {', '.join([c.name for c in expert.capabilities[:3]])}")
            print(f"   Reliability: {expert.reliability*100:.0f}%")
            print()

        # Ask for human confirmation
        while True:
            choice = input(f"Select expert (1-{len(top_candidates)}) or 's' to skip: ").strip().lower()
            if choice == 's':
                logger.info("Human skipped expert selection")
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(top_candidates):
                    selected = top_candidates[idx]
                    logger.info(f"Human selected expert: {selected.name} ({selected.agent_id})")
                    return selected.agent_id
            except ValueError:
                pass
            print("Invalid choice. Please try again.")
    else:
        # Auto-select best match
        selected = top_candidates[0]
        logger.info(f"Auto-selected expert: {selected.name} ({selected.agent_id})")
        return selected.agent_id


def get_expert_info(agent_id: str) -> Optional[Dict[str, Any]]:
    """
    Get detailed information about an expert agent.

    Args:
        agent_id: Expert agent identifier

    Returns:
        Dictionary with expert details, or None if not found
    """
    expert_registry = ExpertAgentRegistry()
    expert = expert_registry.get_agent(agent_id)

    if not expert:
        return None

    return {
        'agent_id': expert.agent_id,
        'name': expert.name,
        'category': expert.category.value,
        'description': expert.description,
        'capabilities': [
            {
                'name': cap.name,
                'description': cap.description,
                'example_use': cap.example_use
            }
            for cap in expert.capabilities
        ],
        'endpoint': expert.endpoint,
        'model_type': expert.model_type,
        'cost_per_call': expert.cost_per_call,
        'avg_latency_ms': expert.avg_latency_ms,
        'reliability': expert.reliability
    }


def create_autogen_expert_wrapper(agent_id: str, config_list: List[Dict],
                                   skill_registry) -> Optional[Any]:
    """
    Create an Autogen ConversableAgent wrapper for an expert agent.

    This allows expert agents to be used directly in Autogen conversations.

    Args:
        agent_id: Expert agent identifier
        config_list: Autogen config list for LLM
        skill_registry: Instance of AgentSkillRegistry

    Returns:
        Autogen ConversableAgent instance, or None if expert not found
    """
    from autogen import ConversableAgent

    expert_registry = ExpertAgentRegistry()
    expert = expert_registry.get_agent(agent_id)

    if not expert:
        logger.error(f"Expert {agent_id} not found")
        return None

    # Create system message from expert profile
    system_message = f"""You are {expert.name}, a specialized AI agent.

{expert.description}

Your capabilities include:
{chr(10).join(f'- {cap.name}: {cap.description}' for cap in expert.capabilities)}

You are part of a dream fulfillment network helping users achieve their goals.
Focus on your area of expertise and provide practical, actionable guidance."""

    # Create Autogen agent
    agent = ConversableAgent(
        name=agent_id,
        system_message=system_message,
        llm_config={
            "config_list": config_list,
            "temperature": 0.7,
            "max_tokens": 1000
        },
        human_input_mode="NEVER"
    )

    # Register agent's skills with skill registry
    skills = []
    for capability in expert.capabilities:
        skills.append({
            'name': capability.name,
            'description': capability.description,
            'proficiency': expert.reliability,
            'metadata': {
                'example_use': capability.example_use,
                'category': expert.category.value
            }
        })
    skill_registry.register_agent(agent_id, skills)

    logger.info(f"Created Autogen wrapper for expert: {expert.name}")
    return agent


def recommend_experts_for_dream(dream_statement: str, top_k: int = 5) -> List[ExpertAgent]:
    """
    Recommend expert agents for achieving a dream.

    This is the high-level function for dream fulfillment.

    Args:
        dream_statement: User's dream in natural language
        top_k: Number of experts to recommend

    Returns:
        List of recommended ExpertAgent instances
    """
    expert_registry = ExpertAgentRegistry()

    # Use the registry's recommendation engine
    # Use "general" as default dream_category for broad search
    recommended = expert_registry.recommend_agents(dream_statement, "general")

    return recommended[:top_k]


# Export main functions
__all__ = [
    'ExpertAgentRegistry',
    'ExpertAgent',
    'AgentCategory',
    'register_all_experts',
    'get_expert_for_task',
    'get_expert_info',
    'create_autogen_expert_wrapper',
    'recommend_experts_for_dream'
]
