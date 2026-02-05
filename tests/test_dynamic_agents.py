"""
Test Dynamic Agent Discovery and Registration

Tests the new dynamic agent system that discovers agents from recipe JSON files.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(__file__))

from integrations.google_a2a import (
    get_dynamic_discovery,
    list_available_agents,
    get_registered_agent_info
)

def test_dynamic_discovery():
    """Test agent discovery from prompts directory"""
    print("="*80)
    print("DYNAMIC AGENT DISCOVERY TEST")
    print("="*80)

    # Get discovery instance
    discovery = get_dynamic_discovery()

    # Discover agents
    print(f"\nScanning prompts/ directory for recipe JSONs...")
    num_agents = discovery.discover_all_agents()

    print(f"\n[OK] Discovered {num_agents} trained agents")

    if num_agents == 0:
        print("\n[WARN] No agents found!")
        print("Expected files: prompts/*_*_recipe.json")
        print("Example: prompts/71_0_recipe.json")
        return False

    # Show details
    print("\n" + "-"*80)
    print("AGENT DETAILS")
    print("-"*80)

    for agent in discovery.get_all_agents():
        print(f"\nAgent ID: {agent.agent_id}")
        print(f"  Prompt: {agent.prompt_id}")
        print(f"  Flow: {agent.flow_id} ({agent.flow_name})")
        print(f"  Persona: {agent.persona}")
        print(f"  Status: {agent.status}")
        print(f"  Action: {agent.action[:80]}...")
        print(f"  Recipe Steps: {len(agent.recipe)}")
        print(f"  Autonomous: {agent.can_perform_without_user_input}")
        print(f"  Has Fallback: {bool(agent.fallback_action)}")

        # Show skills
        skills = discovery.get_agent_skills(agent)
        print(f"  Skills: {len(skills)}")
        for idx, skill in enumerate(skills[:2]):  # Show first 2 skills
            print(f"    {idx+1}. {skill['name']}: {skill['description'][:50]}...")

    return True


def test_agent_grouping():
    """Test agent grouping and statistics"""
    print("\n" + "="*80)
    print("AGENT STATISTICS")
    print("="*80)

    info = get_registered_agent_info()

    print(f"\nTotal Agents: {info['total_agents']}")

    print(f"\nBy Prompt:")
    for prompt_id, agent_ids in info['by_prompt'].items():
        print(f"  Prompt {prompt_id}: {len(agent_ids)} agents - {agent_ids}")

    print(f"\nBy Persona:")
    for persona, agent_ids in info['by_persona'].items():
        print(f"  {persona}: {len(agent_ids)} agents - {agent_ids}")

    print(f"\nBy Status:")
    for status, count in info['by_status'].items():
        print(f"  {status}: {count}")

    print(f"\nAutonomous Agents: {len(info['autonomous_agents'])}")
    if info['autonomous_agents']:
        print(f"  {info['autonomous_agents']}")

    print(f"\nAgents with Fallback: {len(info['agents_with_fallback'])}")
    if info['agents_with_fallback']:
        print(f"  {info['agents_with_fallback']}")


if __name__ == "__main__":
    print("\nTesting Dynamic Agent Discovery System...")
    print("="*80)

    success = test_dynamic_discovery()

    if success:
        test_agent_grouping()

        print("\n" + "="*80)
        print("FORMATTED AGENT LIST")
        print("="*80)
        list_available_agents()

        print("\n[OK] All dynamic agent discovery tests passed!")
        sys.exit(0)
    else:
        print("\n[FAIL] Dynamic agent discovery test failed")
        sys.exit(1)
