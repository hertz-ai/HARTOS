# Thought Experiments

Structured thought experiments with peer voting for constitutional governance.

## Purpose

Thought experiments provide a deliberation mechanism for decisions that affect the entire network. Instead of a single node or operator making a unilateral choice, the proposal is put to a structured vote among participating peers.

## Workflow

1. **Propose** -- A node submits a thought experiment with a title, description, and options.
2. **Vote** -- Peers cast votes using the `ExperimentVote` model. Each peer gets one vote per experiment.
3. **Resolve** -- After the voting period closes, the outcome is determined by majority.

## Constitutional Governance

Certain high-stakes actions require a thought experiment vote before they can proceed:

- **Live trading activation** -- Moving from paper trading to real-money trading requires a constitutional vote (see [trading-agents.md](trading-agents.md)).
- **Policy changes** -- Modifications to compute policies or revenue split ratios.
- **Federation decisions** -- Accepting or rejecting new hivemind federations.

## ExperimentVote Model

The `ExperimentVote` model stores:

- `experiment_id` -- Reference to the thought experiment.
- `voter_id` -- The peer casting the vote.
- `choice` -- The selected option.
- `timestamp` -- When the vote was cast.

## Source Files

- `integrations/social/models.py` (ExperimentVote)
- `integrations/social/services.py` (thought experiment endpoints)
