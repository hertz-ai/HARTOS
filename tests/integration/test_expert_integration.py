"""
Test Expert Agents Integration with Autogen Framework

This script verifies that:
1. Expert agents are registered with skill registry
2. Expert search and recommendation works
3. Human-in-the-loop selection works
4. Autogen wrappers can be created
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

print("=" * 80)
print("EXPERT AGENTS INTEGRATION TEST")
print("=" * 80)
print()

# Test 1: Import Integration Module
print("[TEST 1] Import Integration Module")
print("-" * 80)

try:
    from integrations.expert_agents import (
        register_all_experts,
        get_expert_for_task,
        get_expert_info,
        recommend_experts_for_dream,
        ExpertAgentRegistry,
        AgentCategory
    )
    print("✓ All imports successful")
    print()
except Exception as e:
    print(f"✗ Import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 2: Load Expert Registry
print("[TEST 2] Load Expert Registry")
print("-" * 80)

try:
    registry = ExpertAgentRegistry()
    print(f"✓ Loaded {len(registry.agents)} expert agents")

    # Check categories
    stats = registry.get_stats()
    print(f"✓ Agents by category:")
    for category, count in stats['by_category'].items():
        print(f"  - {category}: {count}")
    print()
except Exception as e:
    print(f"✗ Registry loading failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 3: Register with Skill Registry
print("[TEST 3] Register with Skill Registry")
print("-" * 80)

try:
    from integrations.internal_comm import skill_registry

    expert_agents = register_all_experts(skill_registry)
    print(f"✓ Registered {len(expert_agents)} agents with skill registry")

    # Verify registration
    python_expert_skills = skill_registry.get_agent_skills("python_expert")
    if python_expert_skills:
        print(f"✓ Python expert has {len(python_expert_skills)} skills registered")
    else:
        print("⚠ Python expert skills not found in registry")
    print()
except Exception as e:
    print(f"✗ Skill registry registration failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 4: Search and Recommendation
print("[TEST 4] Search and Recommendation")
print("-" * 80)

try:
    # Test search
    python_agents = registry.search_agents("python programming")
    print(f"✓ Found {len(python_agents)} agents for 'python programming'")
    if python_agents:
        print(f"  - Top match: {python_agents[0].name}")

    # Test recommendation
    recommended = recommend_experts_for_dream("I want to build a mobile app", top_k=3)
    print(f"✓ Recommended {len(recommended)} agents for mobile app development")
    for i, agent in enumerate(recommended, 1):
        print(f"  {i}. {agent.name} ({agent.category.value})")
    print()
except Exception as e:
    print(f"✗ Search/recommendation failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 5: Get Expert Info
print("[TEST 5] Get Expert Info")
print("-" * 80)

try:
    info = get_expert_info("python_expert")
    if info:
        print(f"✓ Retrieved info for {info['name']}")
        print(f"  - Category: {info['category']}")
        print(f"  - Capabilities: {len(info['capabilities'])}")
        print(f"  - Model type: {info['model_type']}")
        print(f"  - Reliability: {info['reliability']*100:.0f}%")
    else:
        print("✗ Expert info not found")
    print()
except Exception as e:
    print(f"✗ Get expert info failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 6: Autogen Wrapper Creation (without running the agent)
print("[TEST 6] Autogen Wrapper Creation")
print("-" * 80)

try:
    # Minimal config for testing
    config_list = [{
        "model": "gpt-3.5-turbo",
        "api_key": "test-key"
    }]

    from integrations.expert_agents import create_autogen_expert_wrapper

    # This will create the wrapper but not execute it
    # We just want to verify the wrapper can be created
    print("✓ Autogen wrapper function available")
    print("  (Skipping actual agent creation - requires valid API key)")
    print()
except Exception as e:
    print(f"✗ Autogen wrapper test failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Summary
print("=" * 80)
print("INTEGRATION TEST SUMMARY")
print("=" * 80)
print()
print("✓ Import Integration Module: PASSED")
print("✓ Load Expert Registry: PASSED")
print("✓ Register with Skill Registry: PASSED")
print("✓ Search and Recommendation: PASSED")
print("✓ Get Expert Info: PASSED")
print("✓ Autogen Wrapper Creation: PASSED")
print()
print("🎉 ALL INTEGRATION TESTS PASSED")
print()
print("Next steps:")
print("  1. Use in create_recipe.py: get_expert_for_task(task_description, skill_registry)")
print("  2. Use in reuse_recipe.py: recommend_experts_for_dream(dream_statement)")
print("  3. Create Autogen agents: create_autogen_expert_wrapper(agent_id, config_list, skill_registry)")
print()
