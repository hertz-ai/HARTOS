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
- Agent or human comes from the voting **account**, never from the request
  (an agent account cannot claim a human's weight)

### One identity, one vote

An agent counts as the person at the root of its owner chain; an agent
no person owns counts as this node, its owner when known (owner ruling
2026-10-21; `voting_rules.voter_identity`, `node_identity`), and the world
model's voter is one identity of its own.  Before the ratio, every identity's votes
collapse into one (`voting_rules.one_vote_per_identity`): the human's own
vote if they cast one, otherwise the majority of their agents' votes; a
FOR/AGAINST tie is no vote.  Ten agents of one person are one vote.

### Approval: the one rule

`voting_rules.approval_verdict(tally)` is the only approval rule; auto-evolve,
the evaluation-goal writer, `decide()` and the tally's recommendation all ask
it.  Approved needs all of:

- **Quorum**: at least 3 distinct identities voted decisively, at least 2 FOR
- **Threshold**: FOR share of the decisive weight >= max(2/3, the context's
  `approval_threshold`); a context can raise the bar (0.8 for
  `security_guardrail`), never lower it
- **Steward**: where the context is `steward_required`, a FOR vote from a
  signed-in human account holding the central (steward) role; an abstain is
  no answer, and `decide()` waits for a FOR or AGAINST

### Evaluating here: on a flat node, this node's own experiments need no vote

Owner rulings: 2026-11-14 *"voting shd not be a prerequisite to evolve
locally untill we cross the intelligence barrier"*; 2026-11-15 *"the no ovite
experiment applies only for topology flat, for regional and central it shd
remain with voites"*.  `voting_rules.evaluation_permit(tally, this_nodes_own)`
is the one rule the evaluation-goal writer and auto-evolve both ask:

- an experiment the vote approved may be evaluated, on every topology;
- otherwise only when ALL hold: the node is flat (`HEVOLVE_NODE_TIER` unset
  or `flat`; anything else keeps the vote), the experiment is this node's
  **own**, its context is not `steward_required` (security stays with
  people), and no vote is against it (a vote against sends it back to the
  vote);
- **own** (`voting_rules.node_holds`): the creator is the owner
  `HEVOLVE_OWNER_USER_ID` names, or a `system` account owned by no one with no
  password (the node's `nunba` account, made by `ensure_system_user`).  Never
  an agent (`/auth/register` makes ownerless agents for anyone and takes
  `owner_id` from the request body) and never a `system` account with a
  password (the same route takes `user_type` from the body);
- a refusal says why (`permit.reason`, `hint`): `not_this_nodes_own`,
  `topology_keeps_the_vote`, `steward_required` or `vote_against`.

Evaluating is local work. `decide()` and anything shared with the hive keep
the vote. The intelligence barrier has no measure yet; when the owner defines
one, `evaluation_permit` is where the vote returns.

### Tally

```python
weighted_score = sum(vote * weight) / sum(weights)   # one vote per identity

decision_recommendation = voting_rules.recommendation(tally):
  approved by approval_verdict            → 'approve'
  no quorum / steward missing             → 'no_quorum' / 'steward_required'
  AGAINST share >= the threshold          → 'reject'
  otherwise                               → 'inconclusive'
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

1. Gather eligible experiments (voted ones, and on a flat node this node's own)
2. Constitutional filter
3. Democratic vote tally (on a flat node this node's own need no vote; see above)
4. Select top-N: vote-approved by score, then this node's own, newest first
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
