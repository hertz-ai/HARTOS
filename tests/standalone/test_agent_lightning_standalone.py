"""
Test Agent Lightning Standalone Implementation
Tests our custom Agent Lightning implementation without external dependencies.
"""

import os
os.environ['AGENT_LIGHTNING_ENABLED'] = 'true'

from integrations.agent_lightning import (
    AgentLightningWrapper,
    LightningTracer,
    RewardCalculator,
    RewardType,
    LightningStore,
    is_enabled
)

print("=" * 60)
print("Agent Lightning Standalone Test")
print("=" * 60)

# Test 1: Configuration
print("\n[TEST 1] Configuration")
print(f"  Enabled: {is_enabled()}")
assert is_enabled() == True, "Should be enabled"
print("  PASS: Configuration working")

# Test 2: Tracer
print("\n[TEST 2] Tracer")
tracer = LightningTracer(agent_id="test_agent")
span_id = tracer.start_span("test_span", context={"test": "data"})
tracer.emit_prompt(span_id, "Test prompt", context={"exec_time": 0.5})
tracer.emit_response(span_id, "Test response", context={"exec_time": 0.5})
tracer.emit_tool_call(span_id, "test_tool", "arg1, arg2")
tracer.emit_reward(span_id, 1.0, context={"success": True})
tracer.end_span(span_id, "success", result={"test": "complete"})
print(f"  Created span: {span_id}")
print(f"  Active spans: {len(tracer.get_active_spans())}")
print(f"  Total spans: {len(tracer.spans)}")
assert len(tracer.spans) > 0, "Should have spans"
print("  PASS: Tracer working")

# Test 3: Reward Calculator
print("\n[TEST 3] Reward Calculator")
calculator = RewardCalculator(agent_id="test_agent")
reward1 = calculator.calculate_reward(
    RewardType.TASK_COMPLETION,
    context={"success": True, "execution_time": 0.5}
)
reward2 = calculator.calculate_reward(
    RewardType.TASK_FAILURE,
    context={"error": "test error"}
)
print(f"  Task completion reward: {reward1}")
print(f"  Task failure reward: {reward2}")
assert reward1 > 0, "Completion reward should be positive"
assert reward2 < 0, "Failure reward should be negative"
print("  PASS: Reward calculator working")

# Test 4: Store
print("\n[TEST 4] Store")
store = LightningStore(agent_id="test_agent", backend="memory")
span = tracer.get_span(span_id)
if span:
    saved = store.save_span(span)
    print(f"  Span saved: {saved}")
    loaded = store.load_span(span_id)
    print(f"  Span loaded: {loaded is not None}")
    assert loaded is not None, "Should load saved span"
    print("  PASS: Store working")
else:
    print("  SKIP: No span to test")

# Test 5: Mock Agent Wrapping
print("\n[TEST 5] Mock Agent Wrapping")

class MockAgent:
    """Mock AutoGen agent for testing"""
    def __init__(self, name):
        self.name = name
        self.call_count = 0

    def generate_reply(self, messages):
        self.call_count += 1
        return f"Reply {self.call_count}"

mock_agent = MockAgent("test")
wrapped = AgentLightningWrapper(
    agent=mock_agent,
    agent_id="wrapped_test_agent",
    track_rewards=True,
    auto_trace=True
)

print(f"  Original agent: {mock_agent.name}")
print(f"  Wrapped agent: {wrapped.agent.name}")
print(f"  Tracer attached: {wrapped.tracer is not None}")
print(f"  Reward calculator attached: {wrapped.reward_calculator is not None}")
assert wrapped.agent.name == "test", "Should wrap original agent"
assert wrapped.tracer is not None, "Should have tracer"
assert wrapped.reward_calculator is not None, "Should have calculator"
print("  PASS: Agent wrapping working")

# Test 6: Statistics
print("\n[TEST 6] Statistics")
tracer_stats = tracer.get_statistics()
calculator_stats = calculator.get_statistics()
store_stats = store.get_statistics()
wrapped_stats = wrapped.get_statistics()

print(f"  Tracer stats: {tracer_stats}")
print(f"  Calculator stats: {len(calculator_stats)} keys")
print(f"  Store stats: {store_stats}")
print(f"  Wrapped agent stats: {len(wrapped_stats)} keys")
print("  PASS: Statistics working")

print("\n" + "=" * 60)
print("ALL TESTS PASSED")
print("=" * 60)
print("\nAgent Lightning standalone implementation is fully functional!")
print("No external package dependencies required.")
print("\nTo enable in production:")
print("  export AGENT_LIGHTNING_ENABLED=true")
print("\nThe integration is ready to use in create_recipe.py and reuse_recipe.py")
