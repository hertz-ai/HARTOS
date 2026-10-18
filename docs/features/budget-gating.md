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

Checks a goal's Spark budget and reserves the estimate while the turn runs.

- Uses a row-level lock on `AgentGoal.spark_budget`.
- If the estimated cost exceeds the remaining budget, the call is rejected.
- The reservation is atomic, so concurrent dispatches cannot overspend.
- It is a pre-check, not work: it is held under its holder (the
  `dispatch_goal` turn, or the speculative expert) and that holder gives it
  back with `release_goal_reservations()` when its work ends. Spark rises
  only on completed work (`charge_goal_work_completed()`), and the
  completion gate reads spark through `spark_spent_on_work()`, which leaves
  anything still held out. Before a dispatch the daemons read it with
  `goal_spark_now()`, as committed at that moment rather than off the row
  their tick loaded, so a reservation given back in the meantime is not
  counted twice. A release that cannot be written is parked and retried by
  the goal's next release, and by the agent daemon at the start of each
  tick.
- A reservation made by another process (the stdio MCP server's
  `dispatch_goal`) is not in this process's ledger, so it reads as work
  until it is given back (task #175).

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
