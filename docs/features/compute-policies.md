# Compute Policies

Compute policies control which models a node is allowed to use and how metered API costs are handled.

## Three Policies

| Policy | Behavior | Cost |
|--------|----------|------|
| **local_only** | Only models with `is_local=True` (llama, mistral, phi, qwen, groq). | Zero Spark cost. |
| **local_preferred** | Try a local model first; fall back to a metered cloud model if no local model can handle the task. Metered costs are tracked and compensated. | Free when local succeeds; tracked when fallback occurs. |
| **any** | Use the fastest available model regardless of locality. All metered costs are tracked. | Metered costs tracked per call. |

## Hive and Idle Task Enforcement

When a node executes tasks on behalf of other users (hive dispatch or idle compute), the policy is enforced as at least **local_preferred** unless the node operator has explicitly opted into **any**. This prevents nodes from silently incurring cloud API costs for other users' workloads.

## Configuration

Compute policy can be set at three levels (highest priority wins):

1. **Environment variable**: `HEVOLVE_COMPUTE_POLICY=local_only|local_preferred|any`
2. **Database**: `NodeComputeConfig` row for the node.
3. **API**: `PUT /api/settings/compute` with a JSON body containing the desired policy.

## Interaction with Budget Gating

The compute policy is checked before `estimate_llm_cost_spark()`. If the policy forbids cloud models and no local model is available, the dispatch is rejected before any budget check occurs. See [budget-gating.md](budget-gating.md).

## Source Files

- `integrations/agent_engine/speculative_dispatcher.py`
- `integrations/agent_engine/budget_gate.py`
- `integrations/service_tools/vram_manager.py`
