# Contribution Scoring

Quantifies each node's contributions to the network for proportional revenue distribution.

## Score Weights

| Metric | Points | Description |
|--------|--------|-------------|
| `uptime_ratio` | 100 per 100% uptime | Linear scale; a node with 50% uptime earns 50 points. |
| `agent_count` | 2 per agent | Number of agents the node hosts and keeps available. |
| `post_count` | 0.5 per post | Social platform content contributed by the node's users. |
| `ad_impressions` | 0.1 per impression | Ad impressions served through the node. |
| `gpu_hours` | 5 per GPU-hour | GPU compute time contributed to the network. |
| `inferences` | 0.01 per inference | Total inference requests processed. |
| `energy_kwh` | 2 per kWh | Electricity consumed while serving the network. |
| `api_costs_absorbed` | 10 per USD | Real money spent on metered APIs for hive/idle tasks. |

## Visibility Tiers

A node's contribution score determines its visibility tier in the network:

| Tier | Minimum Score | Effect |
|------|---------------|--------|
| **standard** | 0 | Default visibility; eligible for basic revenue share. |
| **featured** | 100 | Highlighted in peer discovery; higher priority in task dispatch. |
| **priority** | 500 | Top-tier visibility; first pick for high-value tasks and premium ad placements. |

## Revenue Distribution

The 90% User Pool (see [revenue-model.md](revenue-model.md)) is distributed proportionally to each node's `contribution_score`. A node with twice the score of another receives twice the payout from the user pool.

## Source Files

- `integrations/agent_engine/revenue_aggregator.py`
- `integrations/social/models.py` (PeerNode columns)
