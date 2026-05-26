#!/bin/bash
# ============================================================
# Cloud Mode (Central Server) Startup
# ============================================================
# Non-compute-heavy central server mode.
# Intelligence comes from GPT, Claude, and other cloud LLMs.
# No local llama.cpp needed. Standalone without Nunba app.
# HevolveAI pip-installed for world model bridge.
#
# Usage:
#   ./start_cloud.sh                          (uses OPENAI_API_KEY from .env)
#   ./start_cloud.sh --api-key sk-xxxx        (explicit API key)
#   ./start_cloud.sh --model gpt-4.1          (use full gpt-4.1 instead of mini)
#   ./start_cloud.sh --endpoint https://your-azure.openai.azure.com/v1
# ============================================================

echo "========================================"
echo " Cloud Mode (Central Server)"
echo " GPT / Claude / In-House Models"
echo "========================================"
echo ""

# Parse arguments
CLOUD_ENDPOINT="https://api.openai.com/v1"
CLOUD_MODEL="gpt-4.1-mini"
CLOUD_KEY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --endpoint) CLOUD_ENDPOINT="$2"; shift 2 ;;
        --model) CLOUD_MODEL="$2"; shift 2 ;;
        --api-key) CLOUD_KEY="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# API key: explicit arg > env > .env file
if [ -z "$CLOUD_KEY" ]; then
    if [ -n "$OPENAI_API_KEY" ]; then
        CLOUD_KEY="$OPENAI_API_KEY"
    fi
fi
if [ -z "$CLOUD_KEY" ]; then
    DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ENV_FILE="${DIR}/../.env"
    if [ -f "$ENV_FILE" ]; then
        CLOUD_KEY=$(grep -E "^OPENAI_API_KEY=" "$ENV_FILE" | cut -d= -f2-)
    fi
fi
if [ -z "$CLOUD_KEY" ]; then
    echo "WARNING: No API key found. Set OPENAI_API_KEY in .env or use --api-key"
    echo ""
fi

export HEVOLVE_NODE_TIER=central
export HEVOLVE_ENFORCEMENT_MODE=hard
export HEVOLVE_DEV_MODE=false
export HEVOLVE_LLM_ENDPOINT_URL="${CLOUD_ENDPOINT}"
export HEVOLVE_LLM_MODEL_NAME="${CLOUD_MODEL}"
export HEVOLVE_LLM_API_KEY="${CLOUD_KEY}"
export HEVOLVE_AGENT_ENGINE_ENABLED=true
export ENABLE_FEDERATION=true
# Diarization: auto-started as sidecar. Set HEVOLVE_DIARIZATION_URL to override.
export HEVOLVE_DIARIZATION_URL="${HEVOLVE_DIARIZATION_URL:-ws://localhost:8000/spkdn}"

echo "[MODE]       HEVOLVE_NODE_TIER=central"
echo "[LLM]        Endpoint: ${CLOUD_ENDPOINT}"
echo "[LLM]        Model: ${CLOUD_MODEL}"
echo "[ENGINE]     Agent engine enabled"
echo "[FEDERATION] Federation enabled"
echo ""
echo "No local llama.cpp needed - intelligence from cloud APIs."
echo ""

# Onboard Mindstory SDK routes into Kong (idempotent — safe on every boot)
echo "[KONG] Onboarding Mindstory SDK routes..."
HARTOS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m integrations.gateway.kong_onboard \
    --kong-url "${KONG_ADMIN_URL:-http://localhost:8001}" \
    --upstream "${HEVOLVE_API_URL:-http://localhost:8000}" \
    2>/dev/null && echo "[KONG] SDK routes onboarded" \
    || echo "[KONG] Kong not available — SDK routes will be onboarded when Kong starts"
echo ""

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${DIR}/run.sh"
