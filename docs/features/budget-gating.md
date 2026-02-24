# Budget Gating

Pre-dispatch cost control that prevents LLM calls from exceeding available budgets.

## Gate Functions

### estimate_llm_cost_spark()

Estimates the Spark cost of an LLM call before it happens.

- Calculates token count from the prompt.
- Multiplies by the per-model cost rate.
- Local models (llama, mistral, phi, qwen, groq) have a cost rate of **0 Spark**.
- Cloud models have per-1K-token rates defined in the model registry.

### check_goal_budget()

Checks and atomically deducts from a goal's Spark budget.

- Uses a row-level lock on `AgentGoal.spark_budget`.
- If the estimated cost exceeds the remaining budget, the call is rejected.
- Deduction is atomic to prevent race conditions under concurrent dispatch.

### check_platform_affordability()

Verifies the platform can afford the call at a macro level.

- Computes 7-day net revenue (revenue minus costs).
- Result is cached for 60 seconds to avoid repeated database queries.
- If the platform is running at a net loss, non-essential calls may be throttled.

### pre_dispatch_budget_gate()

The combined gate that runs all three checks in sequence:

1. `estimate_llm_cost_spark()` -- compute the cost.
2. `check_goal_budget()` -- verify the goal can pay.
3. `check_platform_affordability()` -- verify the platform can sustain it.

If any check fails, the LLM call is not dispatched.

## Source Files

- `integrations/agent_engine/budget_gate.py`
- `integrations/agent_engine/speculative_dispatcher.py`
