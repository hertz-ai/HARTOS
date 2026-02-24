#!/bin/bash
# ============================================================
# HART OS - Full Regression Test Runner
# ============================================================
# Runs ALL unit and regression tests across the entire codebase.
# Covers: core, security, channels, social, performance, and
# integration tests (excluding tests that require live services).
#
# CI Mode: CI=true bash scripts/run_regression.sh
#   Skips interactive menu, runs ALL groups, outputs JUnit XML.
# ============================================================

echo "========================================"
echo " HART OS Full Regression Suite"
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
elif command -v python &> /dev/null; then
    PYTHON_EXE="python"
else
    echo "ERROR: Python not found"
    exit 1
fi

echo "Using: $PYTHON_EXE"
echo ""

# ===== OUTPUT DIRECTORIES =====
REPORT_DIR="test-reports"
JUNIT_DIR="$REPORT_DIR/junit"
LOGS_DIR="$REPORT_DIR/logs"
mkdir -p "$JUNIT_DIR" "$LOGS_DIR"

# ===== DEFINE TEST GROUPS =====

# Group 1: Core performance modules (34 tests)
CORE_TESTS="tests/unit/test_core_performance.py"

# Group 2: State machine and lifecycle (12 tests)
STATE_TESTS="tests/unit/test_state_management.py"

# Group 3: Social naming and auth (90+ tests)
SOCIAL_TESTS="tests/unit/test_social_naming.py"

# Group 4: Channel infrastructure (rate limit, dedupe, debounce, retry, security)
CHANNEL_INFRA_TESTS="tests/unit/test_rate_limit.py tests/unit/test_dedupe.py tests/unit/test_debounce.py tests/unit/test_retry.py tests/unit/test_channel_security.py"

# Group 5: Channel adapters
CHANNEL_ADAPTER_TESTS="tests/unit/test_discord_adapter.py tests/unit/test_telegram_adapter.py tests/unit/test_web_adapter.py tests/unit/test_google_chat_adapter.py tests/unit/test_signal_adapter.py tests/unit/test_imessage_adapter.py tests/unit/test_mattermost_adapter.py tests/unit/test_nextcloud_adapter.py"

# Group 6: Session, queue, streaming, preferences
SESSION_TESTS="tests/unit/test_session_manager.py tests/unit/test_message_queue.py tests/unit/test_response_streaming.py tests/unit/test_preferences.py tests/unit/test_builtin_commands.py"

# Group 7: Agent and recipe tests
AGENT_TESTS="tests/unit/test_agent_creation.py tests/unit/test_recipe_generation.py tests/unit/test_reuse_mode.py tests/unit/test_action_execution.py tests/unit/test_scheduler_creation.py"

# Group 8: VLM, coding, shell, file management
TOOL_TESTS="tests/unit/test_vlm_agent.py tests/unit/test_coding_agent.py tests/unit/test_shell_execution.py tests/unit/test_file_manager.py tests/unit/test_file_tracker.py tests/unit/test_qwen3vl_computer_use.py"

# Group 9: Embeddings, memory, image gen, TTS
AI_TESTS="tests/unit/test_embeddings.py tests/unit/test_memory_search.py tests/unit/test_image_gen.py tests/unit/test_tts.py"

# Group 10: Concurrency
CONCURRENCY_TESTS="tests/unit/test_concurrency.py"

# Group 11: Channel e2e regression
CHANNEL_E2E_TESTS="tests/integration/test_channels_e2e_regression.py tests/integration/test_channels_admin_dashboard.py tests/integration/test_channels_gateway_protocol.py tests/integration/test_channels_metrics_collector.py"

# Group 12: WS workstream tests (metered API, compute, budget, revenue)
WS_TESTS="tests/unit/test_budget_gate.py tests/unit/test_boot_hardening.py tests/unit/test_revenue_pipeline.py tests/unit/test_compute_config.py tests/unit/test_model_routing.py tests/unit/test_metered_recovery.py tests/unit/test_settings_api.py tests/unit/test_ad_hosting_rewards.py"

# Group 13: Security + Build verification
SECURITY_TESTS="tests/unit/test_integrity_system.py tests/unit/test_federation_upgrade.py tests/unit/test_build_verification.py tests/unit/test_immutable_audit_log.py tests/unit/test_tool_allowlist.py tests/unit/test_goal_rate_limit.py tests/unit/test_action_classifier.py tests/unit/test_dlp_engine.py"

# ===== CI MODE =====
if [ "${CI:-}" = "true" ] || [ "${CI:-}" = "1" ] || [ "$1" = "--ci" ]; then
    echo "CI MODE: Running ALL test groups"
    echo "========================================"
    TOTAL_PASS=0
    TOTAL_FAIL=0
    TOTAL_ERROR=0
    EXIT_CODE=0

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    LOG_FILE="$LOGS_DIR/regression_ci_${TIMESTAMP}.log"

    run_group() {
        local group_name="$1"
        shift
        echo ""
        echo "--- $group_name ---"
        $PYTHON_EXE -m pytest $@ \
            --noconftest --tb=short --color=no -q \
            --junitxml="$JUNIT_DIR/${group_name// /_}_${TIMESTAMP}.xml" 2>&1 | tee -a "$LOG_FILE"
        local rc=${PIPESTATUS[0]}
        if [ $rc -ne 0 ]; then
            EXIT_CODE=1
        fi
        return $rc
    }

    # Run all groups
    run_group "WS_workstream" $WS_TESTS
    run_group "Security" $SECURITY_TESTS
    run_group "Core" $CORE_TESTS
    run_group "State" $STATE_TESTS
    run_group "Social" $SOCIAL_TESTS
    run_group "Channel_infra" $CHANNEL_INFRA_TESTS
    run_group "Channel_adapters" $CHANNEL_ADAPTER_TESTS
    run_group "Session" $SESSION_TESTS
    run_group "Agent" $AGENT_TESTS
    run_group "Tools" $TOOL_TESTS
    run_group "AI" $AI_TESTS
    run_group "Concurrency" $CONCURRENCY_TESTS
    run_group "Channel_e2e" $CHANNEL_E2E_TESTS

    echo ""
    echo "========================================"
    echo " CI Regression complete (exit code: $EXIT_CODE)"
    echo " JUnit XML: $JUNIT_DIR/"
    echo " Log: $LOG_FILE"
    echo "========================================"
    exit $EXIT_CODE
fi

# ===== INTERACTIVE MODE =====
echo "Select regression scope:"
echo "  1. FULL regression (all test groups)"
echo "  2. Core + Security only (fast)"
echo "  3. Channels only"
echo "  4. Agent + Recipe only"
echo "  5. Quick smoke test (core + state + social)"
echo "  6. WS workstream tests (metered API, compute, revenue)"
echo "  7. Security hardening tests"
echo ""

read -p "Enter choice (1-7): " choice

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
            $WS_TESTS \
            $SECURITY_TESTS \
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
    6)
        echo ""
        echo "Running WS workstream tests..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $WS_TESTS \
            -v --noconftest --tb=short --color=yes
        ;;
    7)
        echo ""
        echo "Running Security hardening tests..."
        echo "========================================"
        $PYTHON_EXE -m pytest \
            $SECURITY_TESTS \
            -v --noconftest --tb=short --color=yes
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
