#!/bin/bash
# ============================================================
# HART OS - End-to-End Test Runner
# ============================================================
# Runs comprehensive E2E tests. Starts server if not running.
# Supports Docker-based and local test modes.
# ============================================================

echo "========================================"
echo " End-to-End Test Suite"
echo "========================================"
echo ""
echo "This will:"
echo "  1. Start API server (if not running)"
echo "  2. Run comprehensive E2E tests"
echo "  3. Test complete agent workflows"
echo "  4. Validate create/reuse recipe pipeline"
echo ""
echo "========================================"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/.."

# ===== PYTHON ENVIRONMENT =====
if [ -f "venv310/bin/python" ]; then
    PYTHON_EXE="./venv310/bin/python"
elif [ -f "venv310/Scripts/python.exe" ]; then
    PYTHON_EXE="./venv310/Scripts/python.exe"
elif command -v python3.10 &> /dev/null; then
    PYTHON_EXE="python3.10"
else
    echo "ERROR: Python 3.10 not found"
    exit 1
fi

# Track if we started the server
SERVER_PID=""

echo ""
echo "Checking if API server is running..."
curl -s http://localhost:6777/status > /dev/null 2>&1

if [ $? -ne 0 ]; then
    echo ""
    echo "API server not running. Starting now..."
    echo "This may take a moment for model loading..."
    echo ""

    $PYTHON_EXE langchain_gpt_api.py &
    SERVER_PID=$!

    echo "Waiting for server to start (PID: $SERVER_PID)..."
    sleep 15

    # Check again
    curl -s http://localhost:6777/status > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo "WARNING: Server may not be ready yet, waiting 15 more seconds..."
        sleep 15
    fi
    echo ""
else
    echo "API server is already running!"
    echo ""
fi

# Graceful shutdown handler
cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$SERVER_PID" ]; then
        echo "Stopping API server (PID: $SERVER_PID)..."
        kill $SERVER_PID 2>/dev/null
        wait $SERVER_PID 2>/dev/null
        echo "Server stopped."
    fi
    exit 0
}
trap cleanup INT TERM

echo "========================================"
echo " Select Test Mode"
echo "========================================"
echo ""
echo "  1. Channel regression tests (91 tests - quick)"
echo "  2. Master test suite (comprehensive)"
echo "  3. Autonomous agent suite (agent workflows)"
echo "  4. Docker E2E tests (requires Docker)"
echo "  5. All local tests (everything)"
echo "  6. Coverage report (with HTML output)"
echo ""

read -p "Enter choice (1-6): " choice

case $choice in
    1)
        echo "Running channel regression tests..."
        $PYTHON_EXE -m pytest tests/integration/test_channels_e2e_regression.py -v -s --tb=short
        ;;
    2)
        echo "Running master test suite..."
        $PYTHON_EXE tests/standalone/test_master_suite.py
        ;;
    3)
        echo "Running autonomous agent suite..."
        $PYTHON_EXE tests/standalone/test_autonomous_agent_suite.py
        ;;
    4)
        echo "Running Docker E2E tests..."
        if ! command -v docker &> /dev/null; then
            echo "ERROR: Docker not found. Install Docker or add to PATH."
            exit 1
        fi
        echo "Building and running Docker test environment..."
        docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
        ;;
    5)
        echo "Running ALL local tests..."
        $PYTHON_EXE -m pytest tests/unit/ tests/integration/ tests/e2e/ -v --tb=short --color=yes
        ;;
    6)
        echo "Running tests with coverage..."
        mkdir -p test-reports/coverage
        $PYTHON_EXE -m pytest tests/integration/test_channels_e2e_regression.py -v --tb=short \
            --cov=integrations/channels \
            --cov-report=html:test-reports/coverage \
            --cov-report=term-missing \
            --cov-config=.coveragerc
        if [ -f "test-reports/coverage/index.html" ]; then
            echo ""
            echo "[SUCCESS] Coverage report generated at test-reports/coverage/index.html"
            # Open in browser (platform-specific)
            if command -v xdg-open &> /dev/null; then
                xdg-open test-reports/coverage/index.html
            elif command -v open &> /dev/null; then
                open test-reports/coverage/index.html
            fi
        fi
        ;;
    *)
        echo "Invalid choice"
        cleanup
        exit 1
        ;;
esac

echo ""
echo "========================================"
echo " E2E Test Complete"
echo "========================================"
echo ""

# Ask if user wants to stop server we started
if [ -n "$SERVER_PID" ]; then
    read -p "Stop API server? (y/n): " stop_server
    if [ "$stop_server" = "y" ] || [ "$stop_server" = "Y" ]; then
        cleanup
    fi
fi
