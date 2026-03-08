# Joining the HART OS Compute Network

How to join the HART OS compute network as a regional compute provider.

## Prerequisites

- A running HART OS instance (`python langchain_gpt_api.py` on port 6777)
- At least one GPU (recommended) or CPU-only node
- Network connectivity to at least one seed peer

## Step 1: Install and Configure

```bash
# Clone and install
git clone <repo-url> && cd HARTOS
python3.10 -m venv venv310
source venv310/Scripts/activate
pip install -r requirements.txt
```

Set environment variables:

```bash
export HEVOLVE_NODE_ID="my-node-001"
export HEVOLVE_NODE_TIER="regional"         # flat | regional | central
export HEVOLVE_BASE_URL="https://my-node.example.com:6777"
```

## Step 2: Configure Cause Alignment

Every provider declares why they are contributing compute. This is gossipped to the network so peers know your motivation.

Supported causes:

| Cause | Description |
|-------|-------------|
| `democratize_compute` | Making AI compute accessible to everyone (default) |
| `frontier_training` | Training the collective HiveMind model |
| `thought_experiments` | Running community-proposed thought experiments |

## Step 3: Join the Network

```bash
curl -X POST http://localhost:6777/api/settings/compute/provider/join \
  -H "Content-Type: application/json" \
  -d '{
    "cause_alignment": "democratize_compute",
    "electricity_rate_kwh": 0.12,
    "offered_gpu_hours_per_day": 8,
    "compute_policy": "local_preferred"
  }'
```

Response:

```json
{
  "joined": true,
  "node_id": "my-node-001",
  "config": {
    "compute_policy": "local_preferred",
    "max_hive_gpu_pct": 50,
    "allow_metered_for_hive": false,
    "offered_gpu_hours_per_day": 8,
    "accept_thought_experiments": true,
    "accept_frontier_training": false
  }
}
```

This creates a `NodeComputeConfig` row and updates your `PeerNode` identity.

## Step 4: Verify Provider Status

```bash
curl http://localhost:6777/api/settings/compute/provider
```

Returns your contribution score, compute stats, pending settlements, and reward summary. See [dashboard.md](dashboard.md) for full details.

## Step 5: Peer Discovery

Once joined, the gossip protocol automatically discovers and connects to other nodes. Seed peers are configured via `HEVOLVE_SEED_PEERS` environment variable. Your cause alignment is included in gossip messages so the network knows your contribution intent.

## What Happens Next

- Your node begins accepting hive tasks based on your `compute_policy`
- GPU hours, inferences, and energy are tracked on your `PeerNode` record
- Metered API costs are recorded as `MeteredAPIUsage` entries
- Settlements are processed automatically (see [settlement.md](settlement.md))
- Your contribution score updates every ~50 seconds via `aggregate_compute_stats()`

## See Also

- [compute-config.md](compute-config.md) -- Fine-tune compute policies
- [dashboard.md](dashboard.md) -- Monitor your contributions
- [settlement.md](settlement.md) -- How compensation works
- [cause.md](cause.md) -- The vision behind compute sharing
