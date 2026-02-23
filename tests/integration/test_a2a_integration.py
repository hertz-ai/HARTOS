"""
Test A2A (Agent-to-Agent) Integration

This script tests the A2A integration by:
1. Testing agent skill registry
2. Testing skill-based agent discovery
3. Testing task delegation
4. Testing context sharing
5. Testing inter-agent messaging

No external servers required - all tests are unit tests
"""

import json
from a2a_protocol import (
    AgentSkill, AgentSkillRegistry, A2AMessage, A2AContextExchange,
    skill_registry, a2a_context, register_agent_with_skills,
    create_delegation_function, create_context_sharing_function,
    create_context_retrieval_function
)


def test_agent_skill_creation():
    """Test 1: Agent Skill Creation"""
    print("=" * 80)
    print("TEST 1: Agent Skill Creation")
    print("=" * 80)

    skill = AgentSkill(
        name="code_execution",
        description="Executing Python code safely",
        proficiency=0.95
    )

    if skill.name == "code_execution" and skill.proficiency == 0.95:
        print("[PASS] Skill created successfully")
        print(f"  Name: {skill.name}")
        print(f"  Description: {skill.description}")
        print(f"  Proficiency: {skill.proficiency}")
        return True
    else:
        print("[FAIL] Skill creation failed")
        return False


def test_skill_registry():
    """Test 2: Skill Registry"""
    print("\n" + "=" * 80)
    print("TEST 2: Skill Registry")
    print("=" * 80)

    registry = AgentSkillRegistry()

    # Register agents with skills
    registry.register_agent('assistant', [
        {'name': 'task_coordination', 'description': 'Coordinating tasks', 'proficiency': 0.95}
    ])

    registry.register_agent('executor', [
        {'name': 'code_execution', 'description': 'Executing code', 'proficiency': 1.0}
    ])

    # Find agents with specific skill
    code_agents = registry.find_agents_with_skill('code_execution')

    if len(code_agents) == 1 and code_agents[0][0] == 'executor':
        print("[PASS] Skill registry working correctly")
        print(f"  Registered agents: {list(registry.agents.keys())}")
        print(f"  Agents with code_execution skill: {[a[0] for a in code_agents]}")
        return True
    else:
        print("[FAIL] Skill registry test failed")
        return False


def test_best_agent_selection():
    """Test 3: Best Agent Selection"""
    print("\n" + "=" * 80)
    print("TEST 3: Best Agent Selection")
    print("=" * 80)

    registry = AgentSkillRegistry()

    # Register multiple agents with same skill but different proficiency
    registry.register_agent('agent1', [
        {'name': 'data_analysis', 'description': 'Analyzing data', 'proficiency': 0.7}
    ])

    registry.register_agent('agent2', [
        {'name': 'data_analysis', 'description': 'Analyzing data', 'proficiency': 0.9}
    ])

    registry.register_agent('agent3', [
        {'name': 'data_analysis', 'description': 'Analyzing data', 'proficiency': 0.8}
    ])

    best_agent = registry.get_best_agent_for_skill('data_analysis')

    if best_agent == 'agent2':
        print("[PASS] Best agent selection working correctly")
        print(f"  Best agent for data_analysis: {best_agent}")
        return True
    else:
        print(f"[FAIL] Expected agent2, got {best_agent}")
        return False


def test_inter_agent_messaging():
    """Test 4: Inter-Agent Messaging"""
    print("\n" + "=" * 80)
    print("TEST 4: Inter-Agent Messaging")
    print("=" * 80)

    registry = AgentSkillRegistry()
    context = A2AContextExchange(registry)

    # Register agents
    context.register_agent('assistant')
    context.register_agent('executor')

    # Send message
    message_id = context.send_message(
        from_agent='assistant',
        to_agent='executor',
        message_type='request',
        content='Please execute this code: print("Hello")'
    )

    # Get messages
    messages = context.get_messages('executor', message_type='request')

    if len(messages) == 1 and messages[0].content == 'Please execute this code: print("Hello")':
        print("[PASS] Inter-agent messaging working correctly")
        print(f"  Message ID: {message_id}")
        print(f"  From: {messages[0].from_agent}")
        print(f"  To: {messages[0].to_agent}")
        print(f"  Content: {messages[0].content}")
        return True
    else:
        print("[FAIL] Messaging test failed")
        return False


def test_context_sharing():
    """Test 5: Context Sharing"""
    print("\n" + "=" * 80)
    print("TEST 5: Context Sharing")
    print("=" * 80)

    registry = AgentSkillRegistry()
    context = A2AContextExchange(registry)

    # Share context
    context.share_context('assistant', 'user_preferences', {'theme': 'dark', 'language': 'en'})

    # Retrieve context
    retrieved = context.get_shared_context('user_preferences')

    if retrieved == {'theme': 'dark', 'language': 'en'}:
        print("[PASS] Context sharing working correctly")
        print(f"  Shared context: {retrieved}")
        return True
    else:
        print("[FAIL] Context sharing test failed")
        return False


def test_task_delegation():
    """Test 6: Task Delegation"""
    print("\n" + "=" * 80)
    print("TEST 6: Task Delegation")
    print("=" * 80)

    registry = AgentSkillRegistry()
    context = A2AContextExchange(registry)

    # Register agents with skills
    registry.register_agent('assistant', [
        {'name': 'task_coordination', 'proficiency': 0.95}
    ])

    registry.register_agent('executor', [
        {'name': 'code_execution', 'proficiency': 1.0}
    ])

    context.register_agent('assistant')
    context.register_agent('executor')

    # Delegate task
    delegation_id = context.delegate_task(
        from_agent='assistant',
        task='Execute Python code to calculate sum',
        required_skills=['code_execution']
    )

    if delegation_id is not None:
        delegation = context.get_delegation_status(delegation_id)
        print("[PASS] Task delegation working correctly")
        print(f"  Delegation ID: {delegation_id}")
        print(f"  From: {delegation['from_agent']}")
        print(f"  To: {delegation['to_agent']}")
        print(f"  Task: {delegation['task']}")
        print(f"  Status: {delegation['status']}")
        return True
    else:
        print("[FAIL] Task delegation failed")
        return False


def test_delegation_completion():
    """Test 7: Delegation Completion"""
    print("\n" + "=" * 80)
    print("TEST 7: Delegation Completion")
    print("=" * 80)

    registry = AgentSkillRegistry()
    context = A2AContextExchange(registry)

    # Register agents
    registry.register_agent('assistant', [{'name': 'task_coordination', 'proficiency': 0.95}])
    registry.register_agent('executor', [{'name': 'code_execution', 'proficiency': 1.0}])

    context.register_agent('assistant')
    context.register_agent('executor')

    # Delegate and complete task
    delegation_id = context.delegate_task(
        from_agent='assistant',
        task='Calculate 1+2+3+...+100',
        required_skills=['code_execution']
    )

    # Simulate completion
    context.complete_delegation(delegation_id, result={'answer': 5050})

    # Check delegation status
    delegation = context.get_delegation_status(delegation_id)

    if delegation['status'] == 'completed' and delegation['result'] == {'answer': 5050}:
        print("[PASS] Delegation completion working correctly")
        print(f"  Delegation Status: {delegation['status']}")
        print(f"  Result: {delegation['result']}")
        return True
    else:
        print("[FAIL] Delegation completion failed")
        return False


def test_delegation_functions():
    """Test 8: Delegation Helper Functions"""
    print("\n" + "=" * 80)
    print("TEST 8: Delegation Helper Functions")
    print("=" * 80)

    # Register test agents
    register_agent_with_skills('test_assistant', [
        {'name': 'task_coordination', 'proficiency': 0.95}
    ])

    register_agent_with_skills('test_executor', [
        {'name': 'code_execution', 'proficiency': 1.0}
    ])

    # Create delegation function
    delegate_func = create_delegation_function('test_assistant')

    # Test delegation
    result = delegate_func(
        task='Execute test code',
        required_skills=['code_execution']
    )

    result_data = json.loads(result)

    if result_data.get('success'):
        print("[PASS] Delegation function working correctly")
        print(f"  Delegation ID: {result_data.get('delegation_id')}")
        print(f"  Message: {result_data.get('message')}")
        return True
    else:
        print("[FAIL] Delegation function failed")
        print(f"  Error: {result_data.get('error')}")
        return False


def test_context_functions():
    """Test 9: Context Sharing Functions"""
    print("\n" + "=" * 80)
    print("TEST 9: Context Sharing Functions")
    print("=" * 80)

    # Register test agent
    register_agent_with_skills('test_context_agent', [
        {'name': 'data_processing', 'proficiency': 0.9}
    ])

    # Create context sharing function
    share_func = create_context_sharing_function('test_context_agent')
    retrieve_func = create_context_retrieval_function()

    # Share context
    share_result = share_func('test_key', {'data': 'test_value'})
    share_data = json.loads(share_result)

    # Retrieve context
    retrieve_result = retrieve_func('test_key')
    retrieve_data = json.loads(retrieve_result)

    if share_data.get('success') and retrieve_data.get('success'):
        print("[PASS] Context sharing functions working correctly")
        print(f"  Shared context: {retrieve_data.get('context_value')}")
        return True
    else:
        print("[FAIL] Context sharing functions failed")
        return False


def test_skill_usage_tracking():
    """Test 10: Skill Usage Tracking"""
    print("\n" + "=" * 80)
    print("TEST 10: Skill Usage Tracking")
    print("=" * 80)

    registry = AgentSkillRegistry()

    # Register agent with skill
    registry.register_agent('tracker_agent', [
        {'name': 'test_skill', 'proficiency': 0.8}
    ])

    # Record skill usage
    registry.record_skill_usage('tracker_agent', 'test_skill', success=True)
    registry.record_skill_usage('tracker_agent', 'test_skill', success=True)
    registry.record_skill_usage('tracker_agent', 'test_skill', success=False)

    # Get skill
    skills = registry.get_agent_skills('tracker_agent')
    skill = skills.get('test_skill')

    if skill and skill.usage_count == 3 and skill.get_success_rate() == (2/3):
        print("[PASS] Skill usage tracking working correctly")
        print(f"  Usage count: {skill.usage_count}")
        print(f"  Success rate: {skill.get_success_rate():.2%}")
        return True
    else:
        print("[FAIL] Skill usage tracking failed")
        return False


def main():
    print("\n" + "=" * 80)
    print("A2A (AGENT-TO-AGENT) INTEGRATION TEST SUITE")
    print("=" * 80 + "\n")

    results = []

    # Run all tests
    results.append(("Agent Skill Creation", test_agent_skill_creation()))
    results.append(("Skill Registry", test_skill_registry()))
    results.append(("Best Agent Selection", test_best_agent_selection()))
    results.append(("Inter-Agent Messaging", test_inter_agent_messaging()))
    results.append(("Context Sharing", test_context_sharing()))
    results.append(("Task Delegation", test_task_delegation()))
    results.append(("Delegation Completion", test_delegation_completion()))
    results.append(("Delegation Functions", test_delegation_functions()))
    results.append(("Context Functions", test_context_functions()))
    results.append(("Skill Usage Tracking", test_skill_usage_tracking()))

    # Print summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f"{status} - {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed ({passed*100//total}%)")

    if passed == total:
        print("\n[OK] All A2A integration tests passed!")
    else:
        print(f"\n[FAIL] {total - passed} test(s) failed")

    return passed == total


if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)
