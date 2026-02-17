#!/bin/bash
# ============================================================
# Regional Mode Startup
# ============================================================
# Networked mode with a regional LLM host.
# Connects to a regional llama.cpp or vLLM server and
# participates in the regional gossip network.
#
# Usage:
#   ./start_regional.sh --host http://regional-server:8080/v1
#   ./start_regional.sh --host http://10.0.1.5:8080/v1 --model Qwen3-VL-4B-Instruct
#   ./start_regional.sh  (reads HEVOLVE_LLM_ENDPOINT_URL from .env)
# ============================================================

echo "========================================"
echo " Regional Mode (Networked LLM Host)"
echo "========================================"
echo ""

# Parse arguments
LLM_HOST=""
LLM_MODEL="Qwen3-VL-4B-Instruct"
LLM_KEY="dummy"

while [[ $# -gt 0 ]]; do
    case $1 in
        --host) LLM_HOST="$2"; shift 2 ;;
        --model) LLM_MODEL="$2"; shift 2 ;;
        --api-key) LLM_KEY="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Validate host
if [ -z "$LLM_HOST" ]; then
    if [ -n "$HEVOLVE_LLM_ENDPOINT_URL" ]; then
        LLM_HOST="$HEVOLVE_LLM_ENDPOINT_URL"
    else
        echo "ERROR: Regional host URL required."
        echo ""
        echo "Usage:"
        echo "  ./start_regional.sh --host http://regional-server:8080/v1"
        echo ""
        echo "Or set HEVOLVE_LLM_ENDPOINT_URL in your .env file."
        exit 1
    fi
fi

export HEVOLVE_NODE_TIER=regional
export HEVOLVE_LLM_ENDPOINT_URL="${LLM_HOST}"
export HEVOLVE_LLM_MODEL_NAME="${LLM_MODEL}"
export HEVOLVE_LLM_API_KEY="${LLM_KEY}"
export HEVOLVE_AGENT_ENGINE_ENABLED=true
# Diarization: auto-started as sidecar. Set URL only to override with external service.
# export HEVOLVE_DIARIZATION_URL="ws://external-host:8004"

echo "[MODE]   HEVOLVE_NODE_TIER=regional"
echo "[LLM]    Endpoint: ${LLM_HOST}"
echo "[LLM]    Model: ${LLM_MODEL}"
echo "[ENGINE] Agent engine enabled"
echo ""

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${DIR}/run.sh"
