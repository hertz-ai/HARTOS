#!/usr/bin/env bash
# ============================================================
# verify_revenue_rails.sh — read-only probe of central revenue stack
# ============================================================
#
# Re-runnable after each step of deploy/enable_revenue_rails.md.
# Touches nothing on the host; only reads.
#
# Usage:
#   ./deploy/verify_revenue_rails.sh                 # probe localhost
#   ./deploy/verify_revenue_rails.sh --remote        # SSH to central
#
# Exit codes:
#   0  every probe passed
#   1  one or more probes failed (details in stdout)
# ============================================================

set -u
PASS=0
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$1"; }

probe() {
    local label="$1"; shift
    if "$@" >/tmp/probe.out 2>&1; then
        green "  [PASS] $label"
        PASS=$((PASS+1))
    else
        red "  [FAIL] $label"
        sed 's/^/         /' /tmp/probe.out | head -5
        FAIL=$((FAIL+1))
    fi
}

REMOTE=false
TARGET_HOST="localhost"
SSH_PORT=422
SSH_USER=sathish

while [[ $# -gt 0 ]]; do
    case $1 in
        --remote) REMOTE=true; TARGET_HOST="etime.hertzai.com"; shift;;
        --host) TARGET_HOST="$2"; shift 2;;
        --port) SSH_PORT="$2"; shift 2;;
        --user) SSH_USER="$2"; shift 2;;
        *) echo "Unknown flag: $1"; exit 2;;
    esac
done

# Prefix all docker / file probes with SSH if --remote
run_remote() {
    if [[ "$REMOTE" == "true" ]]; then
        ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SSH_USER@$TARGET_HOST" "$@"
    else
        eval "$@"
    fi
}

# HTTP probes go to the central URL when --remote; otherwise localhost.
HTTP_BASE="http://localhost:6777"
if [[ "$REMOTE" == "true" ]]; then
    HTTP_BASE="https://etime.hertzai.com"
fi

echo "──────────────────────────────────────────────────────────"
echo " Revenue rail verification — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Target: $TARGET_HOST   HTTP: $HTTP_BASE"
echo "──────────────────────────────────────────────────────────"

# ---- 1. Container alive ----
echo
echo "[1/5] Container & process"
probe "container 'langchain' running" bash -c \
    "run_remote 'sudo docker ps --format \"{{.Names}}\" | grep -qx langchain'"

# ---- 2. Volume mount ----
echo
echo "[2/5] Volume mount for /app/agent_data (Gap #1 from diagnosis)"
probe "/app/agent_data mounted from host" bash -c \
    "run_remote 'sudo docker inspect langchain' | grep -q '/app/agent_data'"

# ---- 3. Env vars present in container ----
echo
echo "[3/5] Payment-rail env vars"
ENV_OUT=$(run_remote 'sudo docker exec langchain env 2>/dev/null' || true)
if echo "$ENV_OUT" | grep -q '^STRIPE_API_KEY=sk_'; then
    green "  [PASS] STRIPE_API_KEY present (and looks like a Stripe key)"
    PASS=$((PASS+1))
else
    yellow "  [WARN] STRIPE_API_KEY not set — Stripe upgrades go through mock gateway"
fi
if echo "$ENV_OUT" | grep -q '^PHONEPE_MERCHANT_ID='; then
    green "  [PASS] PHONEPE_MERCHANT_ID present"
    PASS=$((PASS+1))
else
    yellow "  [WARN] PHONEPE_MERCHANT_ID not set — India rail offline"
fi
if echo "$ENV_OUT" | grep -q '^PHONEPE_SALT_KEY='; then
    green "  [PASS] PHONEPE_SALT_KEY present"
    PASS=$((PASS+1))
else
    yellow "  [WARN] PHONEPE_SALT_KEY not set — India rail offline"
fi

# ---- 4. HTTP endpoints ----
echo
echo "[4/5] HTTP endpoints"
probe "/status responds 200"  curl -fsS --max-time 10 "$HTTP_BASE/status" -o /dev/null
probe "/api/v1/intelligence/pricing responds 200 with tiers" bash -c \
    "curl -fsS --max-time 10 '$HTTP_BASE/api/v1/intelligence/pricing' | grep -q '\"starter\"'"

# Probe both upgrade routes — 401 (unauthenticated) is the expected
# "wired and protected" response.  503 means the gateway isn't
# registered yet.  4xx is good news; 5xx means the route blew up.
PHONEPE_CODE=$(curl -sS -o /dev/null -w "%{http_code}" \
    -X POST -H "Content-Type: application/json" -d '{}' \
    --max-time 10 "$HTTP_BASE/api/v1/intelligence/keys/probe/upgrade/phonepe" || echo "000")
if [[ "$PHONEPE_CODE" == "401" ]]; then
    green "  [PASS] /upgrade/phonepe returns 401 (route alive, auth required)"
    PASS=$((PASS+1))
elif [[ "$PHONEPE_CODE" == "503" ]]; then
    yellow "  [WARN] /upgrade/phonepe returns 503 (PhonePe gateway not registered)"
elif [[ "$PHONEPE_CODE" == "404" ]]; then
    red "  [FAIL] /upgrade/phonepe returns 404 — code not deployed yet"
    FAIL=$((FAIL+1))
else
    red "  [FAIL] /upgrade/phonepe returns $PHONEPE_CODE (unexpected)"
    FAIL=$((FAIL+1))
fi

CALLBACK_CODE=$(curl -sS -o /dev/null -w "%{http_code}" \
    -X POST -H "Content-Type: application/json" -d '{}' \
    --max-time 10 "$HTTP_BASE/api/v1/intelligence/phonepe/callback" || echo "000")
if [[ "$CALLBACK_CODE" == "400" ]] || [[ "$CALLBACK_CODE" == "401" ]] || [[ "$CALLBACK_CODE" == "503" ]]; then
    green "  [PASS] /phonepe/callback returns $CALLBACK_CODE (route alive, rejects empty body)"
    PASS=$((PASS+1))
elif [[ "$CALLBACK_CODE" == "404" ]]; then
    red "  [FAIL] /phonepe/callback returns 404 — code not deployed yet"
    FAIL=$((FAIL+1))
else
    red "  [FAIL] /phonepe/callback returns $CALLBACK_CODE (unexpected)"
    FAIL=$((FAIL+1))
fi

# ---- 5. AP2 ledger persistence ----
echo
echo "[5/5] AP2 ledger persistence"
LEDGER_PATH="/opt/hzai-LLM-Langchain-Chatbot-Agent/mount/agent_data/payment_ledger.json"
probe "payment ledger file exists on host" bash -c \
    "run_remote 'test -f $LEDGER_PATH || test -d $(dirname $LEDGER_PATH)'"

echo
echo "──────────────────────────────────────────────────────────"
if [[ "$FAIL" -eq 0 ]]; then
    green " VERIFICATION: $PASS pass / $FAIL fail — rails ready"
    exit 0
else
    red " VERIFICATION: $PASS pass / $FAIL fail — see [FAIL] lines above"
    exit 1
fi
