#!/bin/bash
# ============================================================
# HevolveBot LangChain Agent - Full Regression Test Runner
# ============================================================
# Runs ALL unit and regression tests across the entire codebase.
# Covers: core, security, channels, social, performance, and
# integration tests (excluding tests that require live services).
# ============================================================

echo "========================================"
echo " HevolveBot Full Regression Suite"
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

echo "Using: $PYTHON_EXE"
echo ""

# ===== DEFINE TEST GROUPS =====

# Group 1: Core performance modules (34 tests)
CORE_TESTS="tests/test_core_performance.py"

# Group 2: State machine and lifecycle (12 tests)
STATE_TESTS="tests/test_state_management.py"

# Group 3: Social naming and auth (90+ tests)
SOCIAL_TESTS="tests/test_social_naming.py"

# Group 4: Channel infrastructure (rate limit, dedupe, debounce, retry, security)
CHANNEL_INFRA_TESTS="tests/test_rate_limit.py tests/test_dedupe.py tests/test_debounce.py tests/test_retry.py tests/test_channel_security.py"

# Group 5: Channel adapters
CHANNEL_ADAPTER_TESTS="tests/test_discord_adapter.py tests/test_telegram_adapter.py tests/test_web_adapter.py tests/test_google_chat_adapter.py tests/test_signal_adapter.py tests/test_imessage_adapter.py tests/test_mattermost_adapter.py tests/test_nextcloud_adapter.py"

# Group 6: Session, queue, streaming, preferences
SESSION_TESTS="tests/test_session_manager.py tests/test_message_queue.py tests/test_response_streaming.py tests/test_preferences.py tests/test_builtin_commands.py"

# Group 7: Agent and recipe tests
AGENT_TESTS="tests/test_agent_creation.py tests/test_recipe_generation.py tests/test_reuse_mode.py tests/test_action_execution.py tests/test_scheduler_creation.py"

# Group 8: VLM, coding, shell, file management
TOOL_TESTS="tests/test_vlm_agent.py tests/test_coding_agent.py tests/test_shell_execution.py tests/test_file_manager.py tests/test_file_tracker.py"

# Group 9: Embeddings, memory, image gen, TTS
AI_TESTS="tests/test_embeddings.py tests/test_memory_search.py tests/test_image_gen.py tests/test_tts.py"

# Group 10: Concurrency
CONCURRENCY_TESTS="tests/test_concurrency.py"

# Group 11: Channel e2e regression
CHANNEL_E2E_TESTS="integrations/channels/tests/test_e2e_regression.py integrations/channels/tests/test_admin_dashboard.py integrations/channels/tests/test_gateway_protocol.py integrations/channels/tests/test_metrics_collector.py"

echo "Select regression scope:"
echo "  1. FULL regression (all test groups)"
echo "  2. Core + Security only (fast)"
echo "  3. Channels only"
echo "  4. Agent + Recipe only"
echo "  5. Quick smoke test (core + state + social)"
echo ""

read -p "Enter choice (1-5): " choice

case $choice in
    1)
        echo ""
        echo "Running FULL regression suite..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $CORE_TESTS \
            $STATE_TESTS \
            $SOCIAL_TESTS \
            $CHANNEL_INFRA_TESTS \
            $CHANNEL_ADAPTER_TESTS \
            $SESSION_TESTS \
            $AGENT_TESTS \
            $TOOL_TESTS \
            $AI_TESTS \
            $CONCURRENCY_TESTS \
            $CHANNEL_E2E_TESTS \
            -v --tb=short --color=yes -q
        ;;
    2)
        echo ""
        echo "Running Core + Security regression..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $CORE_TESTS \
            $STATE_TESTS \
            $SOCIAL_TESTS \
            -v --tb=short --color=yes
        ;;
    3)
        echo ""
        echo "Running Channels regression..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $CHANNEL_INFRA_TESTS \
            $CHANNEL_ADAPTER_TESTS \
            $SESSION_TESTS \
            $CHANNEL_E2E_TESTS \
            -v --tb=short --color=yes
        ;;
    4)
        echo ""
        echo "Running Agent + Recipe regression..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $AGENT_TESTS \
            $TOOL_TESTS \
            -v --tb=short --color=yes
        ;;
    5)
        echo ""
        echo "Running Quick smoke test..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $CORE_TESTS \
            $STATE_TESTS \
            $SOCIAL_TESTS \
            -v --tb=short --color=yes -q
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "========================================"
echo " Regression run complete"
echo "========================================"
