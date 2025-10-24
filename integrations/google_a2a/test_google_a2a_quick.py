"""Quick Google A2A Protocol validation test"""
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

print("Testing Google A2A Protocol imports...")
try:
    from integrations.google_a2a import (
        TaskState, AgentCard, A2ATask, A2AMessageHandler, A2AProtocolServer,
        initialize_a2a_server, get_a2a_server, A2A_PROTOCOL_VERSION,
        register_all_agents, ASSISTANT_SKILLS, HELPER_SKILLS, EXECUTOR_SKILLS, VERIFY_SKILLS
    )
    print("[OK] All imports successful")
except Exception as e:
    print(f"[FAIL] Import error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\nTesting Google A2A Protocol components...")
try:
    # Test 1: Check protocol version
    if A2A_PROTOCOL_VERSION == "0.2.6":
        print(f"[OK] Protocol version: {A2A_PROTOCOL_VERSION}")
    else:
        print(f"[FAIL] Unexpected protocol version: {A2A_PROTOCOL_VERSION}")
        sys.exit(1)

    # Test 2: Verify TaskState enum
    states = [TaskState.SUBMITTED, TaskState.WORKING, TaskState.INPUT_REQUIRED, TaskState.COMPLETED, TaskState.FAILED]
    print(f"[OK] TaskState enum has {len(states)} states")

    # Test 3: Create AgentCard
    agent_card = AgentCard(
        name="TestAgent",
        description="Test agent",
        url="http://localhost:6777/a2a/test",
        version="1.0.0",
        skills=[{"name": "test", "description": "Testing"}]
    )
    card_dict = agent_card.to_dict()
    if card_dict.get("name") == "TestAgent" and card_dict.get("protocolVersion") == "0.2.6":
        print("[OK] AgentCard created successfully")
    else:
        print("[FAIL] AgentCard creation failed")
        sys.exit(1)

    # Test 4: Create A2ATask
    task = A2ATask(
        task_id="test-123",
        message={"parts": [{"type": "text", "text": "test"}]},
        context_id="context-456"
    )
    if task.state == TaskState.SUBMITTED:
        print("[OK] A2ATask created with SUBMITTED state")
    else:
        print(f"[FAIL] Unexpected task state: {task.state}")
        sys.exit(1)

    # Test 5: Update task state
    task.update_state(TaskState.COMPLETED, result={"role": "model", "parts": [{"text": "done"}]})
    if task.state == TaskState.COMPLETED:
        print("[OK] Task state updated to COMPLETED")
    else:
        print(f"[FAIL] Task state not updated: {task.state}")
        sys.exit(1)

    # Test 6: Check all agent skills are defined
    skill_counts = {
        "ASSISTANT": len(ASSISTANT_SKILLS),
        "HELPER": len(HELPER_SKILLS),
        "EXECUTOR": len(EXECUTOR_SKILLS),
        "VERIFY": len(VERIFY_SKILLS)
    }
    print(f"[OK] Agent skills defined: {skill_counts}")

    print("\n[OK] All Google A2A quick tests passed!")
    sys.exit(0)

except Exception as e:
    print(f"[FAIL] Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
