#!/bin/bash
# Test script for distributed HyveOS cluster
# Usage: bash deploy/distributed/test_distributed_cluster.sh
#
# Prerequisite: docker-compose -f deploy/distributed/docker-compose.distributed.yml up -d

set -e

CENTRAL_URL="${HYVE_CENTRAL_URL:-http://localhost:6777}"
WORKER1_URL="${HYVE_WORKER1_URL:-http://localhost:6778}"
WORKER2_URL="${HYVE_WORKER2_URL:-http://localhost:6779}"

echo "=== HyveOS Distributed Cluster Test ==="
echo "Central: $CENTRAL_URL"
echo "Worker1: $WORKER1_URL"
echo "Worker2: $WORKER2_URL"
echo ""

# 1. Health check all nodes
echo "--- Step 1: Health checks ---"
for url in "$CENTRAL_URL" "$WORKER1_URL" "$WORKER2_URL"; do
    status=$(curl -s -o /dev/null -w "%{http_code}" "$url/status" 2>/dev/null || echo "000")
    if [ "$status" = "200" ]; then
        echo "  ✓ $url is healthy"
    else
        echo "  ✗ $url is DOWN (HTTP $status)"
        echo "  Start the cluster first: docker-compose -f deploy/distributed/docker-compose.distributed.yml up -d"
        exit 1
    fi
done
echo ""

# 2. Check distributed endpoints
echo "--- Step 2: Distributed API available ---"
hosts_resp=$(curl -s "$CENTRAL_URL/api/distributed/hosts" 2>/dev/null || echo '{"error":"unavailable"}')
echo "  Hosts response: $hosts_resp"
echo ""

# 3. Register hosts
echo "--- Step 3: Register worker hosts ---"
for node in "node-worker-1:$WORKER1_URL" "node-worker-2:$WORKER2_URL"; do
    IFS=: read -r node_id node_url <<< "$node"
    reg_resp=$(curl -s -X POST "$node_url/api/distributed/hosts/register" \
        -H "Content-Type: application/json" \
        -d "{\"host_id\": \"$node_id\", \"host_url\": \"$node_url\", \"capabilities\": [\"marketing\", \"coding\", \"news\"]}" \
        2>/dev/null || echo '{"error":"failed"}')
    echo "  Register $node_id: $reg_resp"
done
echo ""

# 4. Submit a goal via central
echo "--- Step 4: Submit distributed goal ---"
goal_resp=$(curl -s -X POST "$CENTRAL_URL/api/distributed/goals" \
    -H "Content-Type: application/json" \
    -d '{
        "objective": "Create a marketing post about HyveOS distributed computing",
        "tasks": [
            {"task_id": "mkt_001", "description": "Write Twitter post about distributed AI", "capabilities": ["marketing"]},
            {"task_id": "mkt_002", "description": "Write LinkedIn post about HyveOS features", "capabilities": ["marketing"]}
        ],
        "context": {"goal_type": "marketing", "user_id": "test_user"}
    }' 2>/dev/null || echo '{"error":"failed"}')
echo "  Goal response: $goal_resp"
goal_id=$(echo "$goal_resp" | python3 -c "import sys, json; print(json.load(sys.stdin).get('goal_id', 'unknown'))" 2>/dev/null || echo "unknown")
echo "  Goal ID: $goal_id"
echo ""

# 5. Wait and check if workers claim tasks
echo "--- Step 5: Waiting 10s for workers to claim tasks ---"
sleep 10
if [ "$goal_id" != "unknown" ]; then
    progress=$(curl -s "$CENTRAL_URL/api/distributed/goals/$goal_id/progress" 2>/dev/null || echo '{"error":"failed"}')
    echo "  Progress: $progress"
fi
echo ""

# 6. Check peer discovery
echo "--- Step 6: Peer discovery ---"
peers=$(curl -s "$CENTRAL_URL/api/social/peers/health" 2>/dev/null || echo '{"error":"no peers"}')
echo "  Central health: $peers"
echo ""

echo "=== Test Complete ==="
echo "Check docker logs for worker activity:"
echo "  docker logs hyve-worker-1 --tail 20"
echo "  docker logs hyve-worker-2 --tail 20"
