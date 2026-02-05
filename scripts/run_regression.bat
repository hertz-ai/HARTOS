@echo off
REM ============================================================
REM HevolveBot LangChain Agent - Full Regression Test Runner
REM ============================================================
REM Runs ALL unit and regression tests across the entire codebase.
REM Covers: core, security, channels, social, performance, and
REM integration tests (excluding tests that require live services).
REM ============================================================

echo ========================================
echo  HevolveBot Full Regression Suite
echo ========================================

cd /d %~dp0..

REM ===== PYTHON ENVIRONMENT =====
if exist "venv310\Scripts\python.exe" (
    set PYTHON_EXE=%~dp0..\venv310\Scripts\python.exe
) else if exist "C:\Python310\python.exe" (
    set PYTHON_EXE=C:\Python310\python.exe
) else (
    echo ERROR: Python 3.10 not found
    pause
    exit /b 1
)

echo Using: %PYTHON_EXE%
echo.

REM ===== DEFINE TEST GROUPS =====

REM Group 1: Core performance modules (34 tests)
set CORE_TESTS=tests/test_core_performance.py

REM Group 2: State machine and lifecycle (12 tests)
set STATE_TESTS=tests/test_state_management.py

REM Group 3: Social naming and auth (90+ tests)
set SOCIAL_TESTS=tests/test_social_naming.py

REM Group 4: Channel infrastructure (rate limit, dedupe, debounce, retry, security)
set CHANNEL_INFRA_TESTS=tests/test_rate_limit.py tests/test_dedupe.py tests/test_debounce.py tests/test_retry.py tests/test_channel_security.py

REM Group 5: Channel adapters
set CHANNEL_ADAPTER_TESTS=tests/test_discord_adapter.py tests/test_telegram_adapter.py tests/test_web_adapter.py tests/test_google_chat_adapter.py tests/test_signal_adapter.py tests/test_imessage_adapter.py tests/test_mattermost_adapter.py tests/test_nextcloud_adapter.py

REM Group 6: Session, queue, streaming, preferences
set SESSION_TESTS=tests/test_session_manager.py tests/test_message_queue.py tests/test_response_streaming.py tests/test_preferences.py tests/test_builtin_commands.py

REM Group 7: Agent and recipe tests
set AGENT_TESTS=tests/test_agent_creation.py tests/test_recipe_generation.py tests/test_reuse_mode.py tests/test_action_execution.py tests/test_scheduler_creation.py

REM Group 8: VLM, coding, shell, file management
set TOOL_TESTS=tests/test_vlm_agent.py tests/test_coding_agent.py tests/test_shell_execution.py tests/test_file_manager.py tests/test_file_tracker.py

REM Group 9: Embeddings, memory, image gen, TTS
set AI_TESTS=tests/test_embeddings.py tests/test_memory_search.py tests/test_image_gen.py tests/test_tts.py

REM Group 10: Concurrency
set CONCURRENCY_TESTS=tests/test_concurrency.py

REM Group 11: Channel e2e regression
set CHANNEL_E2E_TESTS=integrations/channels/tests/test_e2e_regression.py integrations/channels/tests/test_admin_dashboard.py integrations/channels/tests/test_gateway_protocol.py integrations/channels/tests/test_metrics_collector.py

echo Select regression scope:
echo   1. FULL regression (all test groups)
echo   2. Core + Security only (fast)
echo   3. Channels only
echo   4. Agent + Recipe only
echo   5. Quick smoke test (core + state + social)
echo.

set /p choice="Enter choice (1-5): "

if "%choice%"=="1" (
    echo.
    echo Running FULL regression suite...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %CORE_TESTS% ^
        %STATE_TESTS% ^
        %SOCIAL_TESTS% ^
        %CHANNEL_INFRA_TESTS% ^
        %CHANNEL_ADAPTER_TESTS% ^
        %SESSION_TESTS% ^
        %AGENT_TESTS% ^
        %TOOL_TESTS% ^
        %AI_TESTS% ^
        %CONCURRENCY_TESTS% ^
        %CHANNEL_E2E_TESTS% ^
        -v --tb=short --color=yes -q
) else if "%choice%"=="2" (
    echo.
    echo Running Core + Security regression...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %CORE_TESTS% ^
        %STATE_TESTS% ^
        %SOCIAL_TESTS% ^
        -v --tb=short --color=yes
) else if "%choice%"=="3" (
    echo.
    echo Running Channels regression...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %CHANNEL_INFRA_TESTS% ^
        %CHANNEL_ADAPTER_TESTS% ^
        %SESSION_TESTS% ^
        %CHANNEL_E2E_TESTS% ^
        -v --tb=short --color=yes
) else if "%choice%"=="4" (
    echo.
    echo Running Agent + Recipe regression...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %AGENT_TESTS% ^
        %TOOL_TESTS% ^
        -v --tb=short --color=yes
) else if "%choice%"=="5" (
    echo.
    echo Running Quick smoke test...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %CORE_TESTS% ^
        %STATE_TESTS% ^
        %SOCIAL_TESTS% ^
        -v --tb=short --color=yes -q
) else (
    echo Invalid choice
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Regression run complete
echo ========================================
pause
