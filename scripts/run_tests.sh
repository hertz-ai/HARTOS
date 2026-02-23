#!/bin/bash
# ============================================================
# HevolveBot LangChain Agent - Interactive Test Runner
# ============================================================
# Runs unit and integration tests with selectable suites.
# ============================================================

echo "========================================"
echo " Running Test Suite"
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

echo ""
echo "Select test suite:"
echo "  1. All tests (comprehensive)"
echo "  2. HevolveBot channel regression tests (91 tests)"
echo "  3. Master test suite"
echo "  4. Autonomous agent suite"
echo "  5. Dynamic agents tests"
echo "  6. Complex agent comprehensive tests"
echo "  7. Quick smoke test (channels only)"
echo "  8. Custom pytest pattern"
echo ""

read -p "Enter choice (1-8): " choice

case $choice in
    1)
        echo "Running all tests..."
        $PYTHON_EXE -m pytest tests/unit/ tests/integration/ -v --tb=short --color=yes
        ;;
    2)
        echo "Running HevolveBot channel regression tests..."
        $PYTHON_EXE -m pytest tests/integration/test_channels_e2e_regression.py -v --tb=short --color=yes
        ;;
    3)
        echo "Running master test suite..."
        $PYTHON_EXE tests/standalone/test_master_suite.py
        ;;
    4)
        echo "Running autonomous agent suite..."
        $PYTHON_EXE tests/standalone/test_autonomous_agent_suite.py
        ;;
    5)
        echo "Running dynamic agents tests..."
        $PYTHON_EXE -m pytest tests/unit/test_dynamic_agents.py -v --tb=short
        ;;
    6)
        echo "Running complex agent comprehensive tests..."
        $PYTHON_EXE -m pytest tests/standalone/test_complex_agent_comprehensive.py -v --tb=short
        ;;
    7)
        echo "Running quick smoke test..."
        $PYTHON_EXE -m pytest tests/integration/test_channels_e2e_regression.py -k "TestModuleImports" -v --tb=short
        ;;
    8)
        read -p "Enter pytest pattern (e.g. tests/test_file.py -k test_name): " pattern
        echo "Running custom pattern..."
        $PYTHON_EXE -m pytest $pattern -v --tb=short --color=yes
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "========================================"
echo " Test run complete"
echo "========================================"
