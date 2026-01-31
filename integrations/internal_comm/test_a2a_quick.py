"""Quick Internal Agent Communication validation test"""
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

print("Testing Internal Agent Communication imports...")
try:
    from integrations.internal_comm import (
        AgentSkill, AgentSkillRegistry, A2AMessage, A2AContextExchange,
        skill_registry, a2a_context, register_agent_with_skills
    )
    print("[OK] All imports successful")
except Exception as e:
    print(f"[FAIL] Import error: {e}")
    sys.exit(1)

print("\nTesting basic Internal Agent Communication functionality...")
try:
    # Test 1: Create skill
    skill = AgentSkill("test", "test skill", 0.9)
    print(f"[OK] Skill created: {skill.name}")

    # Test 2: Register agent
    register_agent_with_skills('test_agent', [
        {'name': 'test_skill', 'description': 'Testing', 'proficiency': 0.9}
    ])
    print("[OK] Agent registered with skills")

    # Test 3: Share context
    a2a_context.share_context('test_agent', 'test_key', 'test_value')
    value = a2a_context.get_shared_context('test_key')
    if value == 'test_value':
        print("[OK] Context sharing works")
    else:
        print("[FAIL] Context sharing failed")
        sys.exit(1)

    print("\n[OK] All quick tests passed!")
    sys.exit(0)

except Exception as e:
    print(f"[FAIL] Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
