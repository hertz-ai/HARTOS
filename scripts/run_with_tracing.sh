#!/bin/bash
# ============================================================
# HART OS - Server WITH Socket Tracing
# ============================================================
# This enables real-time PyCharm socket tracing for debugging.
# Trace events are sent to localhost:5678 for the TrueFlow plugin.
# ============================================================

echo "========================================"
echo " Starting HART OS"
echo " WITH SOCKET TRACING"
echo "========================================"
echo ""

# Get script directory and go to project root
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/.."

# ===== PYTHON ENVIRONMENT =====
if [ -f "venv310/bin/python" ]; then
    PYTHON_EXE="./venv310/bin/python"
    echo "[ENV] Using venv310"
elif [ -f "venv310/Scripts/python.exe" ]; then
    PYTHON_EXE="./venv310/Scripts/python.exe"
    echo "[ENV] Using venv310 (Windows)"
elif command -v python3.10 &> /dev/null; then
    PYTHON_EXE="python3.10"
    echo "[ENV] Using system python3.10"
else
    echo "ERROR: Python 3.10 not found."
    exit 1
fi

# ===== ENABLE PYCHARM SOCKET TRACING =====
export PYTHONPATH="${DIR}/../.pycharm_plugin/runtime_injector:${PYTHONPATH}"
export PYCHARM_PLUGIN_TRACE_ENABLED=1
export PYCHARM_PLUGIN_SOCKET_TRACE=1
export PYCHARM_PLUGIN_TRACE_PORT=5678
export PYCHARM_PLUGIN_TRACE_HOST=127.0.0.1

# Create trace output directory
export TRACE_DIR="${DIR}/../traces"
mkdir -p "${TRACE_DIR}"

echo "[TRACING] Socket trace server will start on localhost:5678"
echo "[TRACING] Connect from PyCharm: Attach to Server button"
echo "[TRACING] PYTHONPATH=${PYTHONPATH}"
echo "[TRACING] Trace Dir: ${TRACE_DIR}"
echo ""

# ===== ENVIRONMENT VARIABLES =====
if [ -f ".env" ]; then
    echo "[ENV] Loading .env file..."
    set -a
    source .env
    set +a
fi

# WAMP/Crossbar connection
export WAMP_URL="${WAMP_URL:-ws://azurekong.hertzai.com:8088/ws}"
export WAMP_REALM="${WAMP_REALM:-realm1}"
echo "[WAMP] URL: $WAMP_URL"
echo "[WAMP] Realm: $WAMP_REALM"
echo ""

# UTF-8 support
export PYTHONUTF8=1
export PYTHONIOENCODING=UTF-8
export PYTHONUNBUFFERED=1

# ===== AGENT LIGHTNING TRACING =====
export AGENT_LIGHTNING_ENABLED=true
export AGENT_LIGHTNING_TRACE_DIR="${DIR}/../agent_data/lightning_traces"
mkdir -p "${AGENT_LIGHTNING_TRACE_DIR}"
echo "[LIGHTNING] Trace dir: ${AGENT_LIGHTNING_TRACE_DIR}"

# ===== SIMPLEMEM LONG-TERM MEMORY =====
export SIMPLEMEM_ENABLED=true
if [ -z "$SIMPLEMEM_API_KEY" ]; then
    echo "[SIMPLEMEM] SIMPLEMEM_API_KEY not set - will use local DB mode"
else
    echo "[SIMPLEMEM] Long-term memory enabled with API key"
fi

# ===== DEPENDENCY CHECK =====
echo "[CHECK] Verifying critical dependencies..."
$PYTHON_EXE -c "import langchain; import autogen; import chromadb; print('  langchain:', langchain.__version__); print('  autogen:', autogen.__version__); print('  chromadb:', chromadb.__version__)" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "WARNING: Some dependencies missing."
    echo ""
fi
echo ""

# ===== START SERVER =====
echo "========================================"
echo " Server Configuration"
echo "========================================"
echo " API:        http://localhost:6777"
echo " Health:     http://localhost:6777/status"
echo " Tracing:    localhost:5678 (PyCharm TrueFlow)"
echo " Chat:       POST http://localhost:6777/chat"
echo " Time Agent: POST http://localhost:6777/time_agent"
echo " VLM Agent:  POST http://localhost:6777/visual_agent"
echo "========================================"
echo ""
echo "Starting server with tracing..."
echo "Press Ctrl+C to stop."
echo ""

$PYTHON_EXE hart_intelligence_entry.py

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Server failed to start"
    echo "Check the error messages above"
    exit 1
fi
