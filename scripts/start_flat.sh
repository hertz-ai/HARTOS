#!/bin/bash
# ============================================================
# Flat Mode (Local) Startup
# ============================================================
# Desktop/development mode with local llama.cpp inference.
# Llama.cpp should be running on localhost (default port 8080)
# or started by Nunba desktop app.
#
# Usage:
#   ./start_flat.sh                   (default port 8080)
#   ./start_flat.sh --llm-port 8081   (custom llama.cpp port)
# ============================================================

echo "========================================"
echo " Flat Mode (Local llama.cpp)"
echo "========================================"
echo ""

# Parse optional --llm-port argument
LLM_PORT=8080
while [[ $# -gt 0 ]]; do
    case $1 in
        --llm-port) LLM_PORT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

export HEVOLVE_NODE_TIER=flat
export LLAMA_CPP_PORT="${LLM_PORT}"

echo "[MODE] HEVOLVE_NODE_TIER=flat"
echo "[LLM]  llama.cpp on localhost:${LLM_PORT}"
echo ""

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${DIR}/run.sh"
