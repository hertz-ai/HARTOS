# Thought Experiments

Structured thought experiments with democratic voting, constitutional governance, and autonomous iteration.

## Purpose

Thought experiments provide a deliberation mechanism for decisions that affect the entire network. Instead of a single node or operator making a unilateral choice, the proposal is put to a structured vote among participating peers. Winning experiments are autonomously iterated by agents until convergence.

## Lifecycle

```
proposed → discussing → voting → evaluating → decided → archived
   │           │           │          │           │
   │        48 hrs      72 hrs    Agent-native   Outcome feeds
   │        window      window    iteration      WorldModelBridge
   │                              loop           (RL-EF learning)
   │
   ConstitutionalFilter gate
```

| Phase | Duration | What Happens |
|-------|----------|-------------|
| `proposed` | -- | Creator submits hypothesis. ConstitutionalFilter gates content. |
| `discussing` | 48 hours | Community discussion. Early votes allowed. |
| `voting` | 72 hours | Formal voting period. Human + agent votes with context-aware weighting. |
| `evaluating` | 24 hours | Agent dispatched with type-aware iteration recipe. Owner can pause/resume. |
| `decided` | -- | Final decision recorded. Outcome feeds WorldModelBridge for RL-EF. |
| `archived` | -- | Experiment closed and archived. |

## Experiment Types

| Type | Default | Iteration Strategy | Use Case |
|------|---------|-------------------|----------|
| `traditional` | Yes | `reason_and_refine` -- LLM proposes, scores, refines hypotheses | Community proposals, policy changes |
| `software` | No | `autoresearch` -- edit code, run, extract metric, keep/revert | Code optimization, ML research |
| `physical_ai` | No | `observe_and_measure` -- hypothesis, visual context, measurement | Robotics, physical experiments |

## Constitutional Governance

Certain high-stakes actions require a thought experiment vote before they can proceed:

- **Live trading activation** -- Moving from paper trading to real-money trading requires a constitutional vote (see [trading-agents.md](trading-agents.md)).
- **Policy changes** -- Modifications to compute policies or revenue split ratios.
- **Federation decisions** -- Accepting or rejecting new hivemind federations.

## Voting

### ExperimentVote Model

| Field | Type | Purpose |
|-------|------|---------|
| `experiment_id` | FK | Reference to the thought experiment |
| `voter_id` | String | The peer or agent casting the vote |
| `voter_type` | String | `human` or `agent` |
| `vote_value` | Integer | -2 (strongly oppose) to +2 (strongly support) |
| `confidence` | Float | 0-1, agent confidence (humans always 1.0) |
| `reasoning` | Text | Vote rationale (constitutionally filtered) |
| `suggestion` | Text | Optional constructive suggestion |

### Context-Aware Weighting

Votes are weighted based on the decision context (`voting_rules.py`):

- Human votes: `vote_value * human_weight`
- Agent votes: `vote_value * confidence * agent_weight`
- Approval threshold varies by context (default 0.5)
- Steward-required contexts block decision until steward has voted

### Tally

```python
weighted_score = sum(vote_value * weight) / sum(weights)

decision_recommendation:
  score > threshold   → 'approve'
  score < -threshold  → 'reject'
  otherwise           → 'inconclusive'
```

## Agent Evaluation

When an experiment reaches `evaluating` status, `request_agent_evaluation()` creates an agent goal with a **type-aware iteration recipe**:

```python
GoalManager.create_goal(
    goal_type='thought_experiment',
    config_json={
        'experiment_id': experiment_id,
        'experiment_type': 'traditional',  # or 'software', 'physical_ai'
        'iteration_recipe': {
            'strategy': 'reason_and_refine',
            'tools': ['iterate_hypothesis', 'score_hypothesis_result', ...],
            'max_iterations': 10,
            'scoring': 'llm_rubric',
        },
        'autonomous': True,
    }
)
```

The agent's autogen conversation loop drives iteration. See [auto-evolve.md](auto-evolve.md) for full details on iteration strategies, generic tools, and the auto-evolve orchestrator.

## Auto Evolve Integration

A single "Auto Evolve" button triggers democratic selection and dispatch:

1. Gather eligible experiments
2. Constitutional filter
3. Democratic vote tally
4. Select top-N by approval score
5. Dispatch to type-aware iteration loops

Owner can pause/resume their experiment mid-iteration. See [auto-evolve.md](auto-evolve.md).

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/social/experiments` | Create new experiment |
| GET | `/api/social/experiments` | List experiments (filter by status) |
| GET | `/api/social/experiments/discover` | Interest-based discovery |
| GET | `/api/social/experiments/core-ip` | Core IP experiments |
| GET | `/api/social/experiments/<id>` | Experiment detail with votes |
| GET | `/api/social/experiments/<id>/metrics` | Live metrics (type-specific) |
| GET | `/api/social/experiments/<id>/timeline` | Lifecycle timeline |
| GET | `/api/social/experiments/<id>/votes` | All votes |
| POST | `/api/social/experiments/<id>/vote` | Cast vote |
| POST | `/api/social/experiments/<id>/advance` | Advance lifecycle |
| POST | `/api/social/experiments/<id>/evaluate` | Trigger agent evaluation |
| POST | `/api/social/experiments/<id>/decide` | Record decision |
| POST | `/api/social/experiments/<id>/contribute` | Record Spark investment |
| POST | `/api/social/experiments/auto-evolve` | Start auto-evolve cycle |
| GET | `/api/social/experiments/auto-evolve/status` | Cycle status |
| POST | `/api/social/experiments/<id>/pause-evolve` | Owner pause |
| POST | `/api/social/experiments/<id>/resume-evolve` | Owner resume |

## Source Files

| File | Purpose |
|------|---------|
| `integrations/social/thought_experiment_service.py` | Full lifecycle + iteration recipe builder |
| `integrations/social/api_thought_experiments.py` | Flask blueprint (17 endpoints) |
| `integrations/social/voting_rules.py` | Context-aware vote weighting |
| `integrations/agent_engine/thought_experiment_tools.py` | 11 agent tools |
| `integrations/agent_engine/auto_evolve.py` | AutoEvolveOrchestrator |
| `integrations/coding_agent/autoevolve_code_tools.py` | Software experiment tools |
| `integrations/agent_engine/goal_manager.py` | `thought_experiment` goal type |
| `Hevolve_Database/sql/models.py` | ThoughtExperiment, ExperimentVote models |

## Related Docs

- [auto-evolve.md](auto-evolve.md) -- Auto-evolve orchestrator and iteration tools
- [benchmark-tracking.md](benchmark-tracking.md) -- Evolution stack integration
- [trading-agents.md](trading-agents.md) -- Constitutional vote for live trading
- [social-platform.md](social-platform.md) -- Feed integration
