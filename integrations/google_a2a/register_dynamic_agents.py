"""
Dynamic Agent Registration for Google A2A Protocol

Replaces hardcoded agent registration with dynamic discovery.
All agents are loaded from prompts/{prompt_id}_{flow_id}_{role_number}_recipe.json files.

NO HARDCODED AGENTS!
"""

import logging
from typing import Dict, Any
from .google_a2a_integration import get_a2a_server
from .dynamic_agent_registry import (
    get_dynamic_discovery,
    get_dynamic_executor,
    TrainedAgent,
    DynamicAgentDiscovery
)

logger = logging.getLogger(__name__)


def create_dynamic_executor_function(agent: TrainedAgent):
    """
    Create an executor function for a dynamically discovered agent

    Args:
        agent: TrainedAgent instance

    Returns:
        Async executor function compatible with A2A protocol
    """
    async def executor(message: str, context_id: str) -> Dict[str, Any]:
        """Execute task for dynamically discovered agent"""
        try:
            executor = get_dynamic_executor()
            result = await executor.execute_agent_task(agent.agent_id, message, context_id)
            return result

        except Exception as e:
            logger.error(f"Dynamic agent {agent.agent_id} execution error: {e}")
            return {
                "role": "model",
                "parts": [{"text": f"Error executing {agent.agent_id}: {str(e)}"}]
            }

    # Set function name for debugging
    executor.__name__ = f"{agent.agent_id}_executor"
    return executor


def register_all_dynamic_agents():
    """
    Discover and register all agents from prompts directory with A2A protocol

    This function:
    1. Scans prompts/ for *_*_*_recipe.json files
    2. Extracts agent capabilities from each recipe
    3. Creates A2A Agent Cards
    4. Registers with Google A2A Protocol server

    Returns:
        Number of agents registered
    """
    try:
        a2a_server = get_a2a_server()

        if a2a_server is None:
            logger.warning("A2A server not initialized, skipping dynamic agent registration")
            return 0

        logger.info("Starting dynamic agent registration...")

        # Discover all trained agents
        discovery = get_dynamic_discovery()
        num_discovered = discovery.discover_all_agents()

        if num_discovered == 0:
            logger.warning("No trained agents found in prompts directory")
            return 0

        # Register each discovered agent
        registered_count = 0

        for agent in discovery.get_all_agents():
            try:
                # Get agent's skills
                skills = discovery.get_agent_skills(agent)

                # Get agent description
                description = discovery.get_agent_description(agent)

                # Create agent name
                agent_name = f"{agent.persona.title()} Agent {agent.agent_id}"

                # Create executor function for this agent
                executor_func = create_dynamic_executor_function(agent)

                # Determine capabilities
                capabilities = {
                    "streaming": False,
                    "async": True,
                    "autonomous": agent.can_perform_without_user_input == "yes",
                    "has_fallback": bool(agent.fallback_action),
                    "recipe_steps": len(agent.recipe)
                }

                # Register with A2A
                a2a_server.register_agent(
                    agent_id=agent.agent_id,
                    name=agent_name,
                    description=description,
                    skills=skills,
                    executor_func=executor_func,
                    capabilities=capabilities
                )

                logger.info(f"Registered dynamic agent: {agent.agent_id} ({agent.persona})")
                registered_count += 1

            except Exception as e:
                logger.error(f"Failed to register agent {agent.agent_id}: {e}")
                continue

        logger.info(f"Successfully registered {registered_count}/{num_discovered} dynamic agents with A2A")
        return registered_count

    except Exception as e:
        logger.error(f"Dynamic agent registration failed: {e}")
        import traceback
        traceback.print_exc()
        return 0


def get_registered_agent_info() -> Dict[str, Any]:
    """
    Get information about all registered dynamic agents

    Returns:
        Dict with agent statistics and details
    """
    discovery = get_dynamic_discovery()
    agents = discovery.get_all_agents()

    # Group by prompt_id
    by_prompt = {}
    for agent in agents:
        if agent.prompt_id not in by_prompt:
            by_prompt[agent.prompt_id] = []
        by_prompt[agent.prompt_id].append(agent)

    # Group by persona
    by_persona = {}
    for agent in agents:
        if agent.persona not in by_persona:
            by_persona[agent.persona] = []
        by_persona[agent.persona].append(agent)

    # Count by status
    by_status = {}
    for agent in agents:
        status = agent.status
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "total_agents": len(agents),
        "by_prompt": {
            str(pid): [a.agent_id for a in agents_list]
            for pid, agents_list in by_prompt.items()
        },
        "by_persona": {
            persona: [a.agent_id for a in agents_list]
            for persona, agents_list in by_persona.items()
        },
        "by_status": by_status,
        "agent_ids": [a.agent_id for a in agents],
        "autonomous_agents": [a.agent_id for a in agents if a.can_perform_without_user_input == "yes"],
        "agents_with_fallback": [a.agent_id for a in agents if a.fallback_action]
    }


def list_available_agents():
    """
    Print formatted list of all available dynamic agents

    Useful for debugging and status checking
    """
    discovery = get_dynamic_discovery()
    agents = discovery.get_all_agents()

    print("\n" + "="*80)
    print("DYNAMICALLY REGISTERED AGENTS")
    print("="*80)

    if not agents:
        print("No agents discovered. Check prompts/ directory for *_*_*_recipe.json files.")
        return

    # Group by prompt
    by_prompt = {}
    for agent in agents:
        if agent.prompt_id not in by_prompt:
            by_prompt[agent.prompt_id] = []
        by_prompt[agent.prompt_id].append(agent)

    for prompt_id in sorted(by_prompt.keys()):
        prompt_def = discovery.prompt_definitions.get(prompt_id, {})
        prompt_name = prompt_def.get("name", f"Prompt {prompt_id}")

        print(f"\n[Prompt {prompt_id}] {prompt_name}")
        print("-" * 80)

        for agent in sorted(by_prompt[prompt_id], key=lambda a: (a.flow_id, a.role_number)):
            status_icon = "✓" if agent.status == "done" else "○"
            auto_icon = "⚡" if agent.can_perform_without_user_input == "yes" else "👤"
            fallback_icon = "🔄" if agent.fallback_action else "  "

            print(f"  {status_icon} {agent.agent_id:15} | "
                  f"Persona: {agent.persona:15} | "
                  f"{auto_icon} | {fallback_icon} | "
                  f"Steps: {len(agent.recipe)}")

    print("\n" + "="*80)
    print(f"Total: {len(agents)} agents")
    print(f"Legend: ✓=Done ○=Pending ⚡=Autonomous 👤=User-Interactive 🔄=Has-Fallback")
    print("="*80 + "\n")
