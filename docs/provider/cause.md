# Cause Alignment

Every provider in the HART OS compute network declares a cause -- the reason they are contributing compute resources. This declaration is gossipped to the network so peers understand each other's motivations.

## The Three Causes

### Democratize Compute

Making AI compute accessible to everyone, regardless of their financial means. Providers who align with this cause contribute GPU hours so that users who cannot afford cloud APIs can still run inference, train agents, and participate in the platform.

This is the default cause for new providers.

### Frontier Training

Training the collective HiveMind model. Providers aligned with this cause contribute their GPU hours specifically for federated learning -- aggregating learning deltas across nodes to build a shared intelligence.

The `FederatedAggregator` coordinates this: extracting local deltas, broadcasting to peers, and running weighted FedAvg aggregation. Convergence is tracked via variance-based scoring.

### Thought Experiments

Running community-proposed thought experiments. The `ThoughtExperiment` table stores proposals; the `ExperimentVote` table records community voting. Providers who accept thought experiments (`accept_thought_experiments: true` in `NodeComputeConfig`) allow their compute to be used for experiments that the community has voted to run.

## Setting Your Cause

At join time:

```bash
curl -X POST http://localhost:6777/api/settings/compute/provider/join \
  -H "Content-Type: application/json" \
  -d '{"cause_alignment": "frontier_training"}'
```

After joining:

```bash
curl -X PUT http://localhost:6777/api/settings/compute \
  -H "Content-Type: application/json" \
  -d '{"cause_alignment": "thought_experiments"}'
```

## Network Visibility

Cause alignment is stored on `PeerNode.cause_alignment` and is included in gossip payloads. This means:

- Other nodes can see why you are contributing
- The network can route tasks to nodes aligned with the relevant cause
- Contribution scoring can weight cause-aligned compute higher

## The Vision

HART OS exists because humans must always control AI. The compute network is a practical expression of this principle: instead of concentrating GPU power in a few cloud providers, we distribute it across a network of individuals and organizations who share the belief that AI should serve humanity.

The three causes are not exclusive -- they are complementary facets of the same mission. Democratizing compute makes AI accessible. Training the frontier model makes AI better. Running thought experiments makes AI safer.

## See Also

- [joining.md](joining.md) -- How to join the network
- [compute-config.md](compute-config.md) -- Configure what tasks you accept
