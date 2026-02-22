@echo off
REM ============================================================
REM HevolveBot — Master Regression Test Runner
REM ============================================================
REM Runs ALL 2700+ unit/integration tests across the codebase.
REM
REM Test groups:
REM   P2P & Security       ~722 tests  (hierarchy, integrity, agent engine, etc.)
REM   Social Platform       ~148 tests  (feed, search, karma, models, etc.)
REM   Channel Infra         ~120 tests  (rate limit, dedupe, debounce, security)
REM   Channel Adapters      ~200 tests  (discord, telegram, web, signal, etc.)
REM   Channel E2E           ~172 tests  (regression, dashboard, gateway, metrics)
REM   Agent & Recipe        ~200 tests  (create, reuse, recipe, scheduler, etc.)
REM   Session & Messaging    ~90 tests  (session, queue, streaming, preferences)
REM   Tools & AI            ~250 tests  (VLM, coding, embeddings, TTS, vision)
REM   Core & Performance    ~150 tests  (core perf, naming, state, concurrency)
REM   Integration            ~50 tests  (integration, redis ledger)
REM   Distro & Hardening   ~469 tests  (distro configs, PXE, OTA, provisioner, security)
REM
REM Standalone scripts (not included — run directly with python):
REM   test_nested_task_system.py, test_nested_tasks.py,
REM   test_master_suite.py, test_agent_lightning_standalone.py,
REM   run_integration_tests.py, run_manual_tests.py
REM
REM E2E tests requiring a live server: scripts/run_e2e_tests.bat
REM ============================================================

echo ========================================
echo  HevolveBot Master Regression Suite
echo  2263 tests across 61 test files
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

REM ===== WINDOWS CONSOLE FIX =====
REM Prevent "Error in sys.excepthook:" crash on Windows console
set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>&1

REM ===== DEFINE TEST GROUPS =====

REM Group 1: P2P Network, Security, Agent Engine (~960+ tests)
set P2P_SECURITY_TESTS=^
    tests/test_hierarchy_system.py ^
    tests/test_integrity_system.py ^
    tests/test_ad_hosting_rewards.py ^
    tests/test_agent_network_resilience.py ^
    tests/test_master_key_system.py ^
    tests/test_coding_agent.py ^
    tests/test_cache_restoration.py ^
    tests/test_agent_engine.py ^
    tests/test_node_watchdog.py ^
    tests/test_auto_discovery.py ^
    tests/test_agent_dashboard.py ^
    tests/test_mode_aware_inference.py ^
    tests/test_system_requirements.py ^
    tests/test_commercial_ip_builds.py ^
    tests/test_federation_upgrade.py ^
    tests/test_continual_learner_gate.py ^
    tests/test_gradient_sync.py ^
    tests/test_thought_experiments.py

REM Group 2: Social Platform (~148 tests)
set SOCIAL_TESTS=^
    tests/test_social_regression.py ^
    tests/test_social_models.py ^
    tests/test_social_feed.py ^
    tests/test_social_search.py ^
    tests/test_social_karma.py ^
    tests/test_social_api.py ^
    tests/test_social_naming.py

REM Group 3: Channel Infrastructure (~120 tests)
set CHANNEL_INFRA_TESTS=^
    tests/test_rate_limit.py ^
    tests/test_dedupe.py ^
    tests/test_debounce.py ^
    tests/test_retry.py ^
    tests/test_channel_security.py ^
    tests/test_channel_integration.py

REM Group 4: Channel Adapters (~200 tests)
set CHANNEL_ADAPTER_TESTS=^
    tests/test_discord_adapter.py ^
    tests/test_telegram_adapter.py ^
    tests/test_web_adapter.py ^
    tests/test_google_chat_adapter.py ^
    tests/test_signal_adapter.py ^
    tests/test_imessage_adapter.py ^
    tests/test_mattermost_adapter.py ^
    tests/test_nextcloud_adapter.py

REM Group 5: Channel E2E (~172 tests)
set CHANNEL_E2E_TESTS=^
    integrations/channels/tests/test_e2e_regression.py ^
    integrations/channels/tests/test_admin_dashboard.py ^
    integrations/channels/tests/test_gateway_protocol.py ^
    integrations/channels/tests/test_metrics_collector.py

REM Group 6: Agent and Recipe Pipeline (~230+ tests)
set AGENT_RECIPE_TESTS=^
    tests/test_agent_creation.py ^
    tests/test_recipe_generation.py ^
    tests/test_reuse_mode.py ^
    tests/test_action_execution.py ^
    tests/test_scheduler_creation.py ^
    tests/test_autonomous_agent_suite.py ^
    tests/test_complex_agent_comprehensive.py ^
    tests/test_dynamic_agents.py ^
    tests/test_nested_tasks_direct.py ^
    tests/test_complete_integration.py ^
    tests/test_complete_e2e_integration.py ^
    tests/test_e2e_pipelines.py ^
    tests/test_recipe_experience_healing.py

REM Group 7: Session and Messaging (~90 tests)
set SESSION_TESTS=^
    tests/test_session_manager.py ^
    tests/test_message_queue.py ^
    tests/test_response_streaming.py ^
    tests/test_preferences.py ^
    tests/test_builtin_commands.py

REM Group 8: Tools and AI (~320+ tests)
set TOOLS_AI_TESTS=^
    tests/test_vlm_agent.py ^
    tests/test_shell_execution.py ^
    tests/test_file_manager.py ^
    tests/test_file_tracker.py ^
    tests/test_vision_sidecar.py ^
    tests/test_embeddings.py ^
    tests/test_memory_search.py ^
    tests/test_image_gen.py ^
    tests/test_tts.py ^
    tests/test_runtime_tools.py

REM Group 9: Core, Performance, and Concurrency (~150 tests)
set CORE_PERF_TESTS=^
    tests/test_core_performance.py ^
    tests/test_state_management.py ^
    tests/test_concurrency.py

REM Group 10: Integration and Data (~50 tests)
set INTEGRATION_TESTS=^
    tests/test_integration.py ^
    tests/test_redis_ledger.py

REM Group 11: Distro, Security Hardening, Deployment (~469 tests)
set DISTRO_TESTS=^
    tests/test_distro_configs.py ^
    tests/test_distro_tools.py ^
    tests/test_pxe_server.py ^
    tests/test_ota_update.py ^
    tests/test_network_provisioner.py ^
    tests/test_security_hardening_distro.py ^
    tests/test_deployment_modes.py

echo Select regression scope:
echo.
echo   1. FULL regression (all 2700+ tests)
echo   2. P2P Network + Security (722 tests - core infrastructure)
echo   3. Social Platform (148 tests)
echo   4. All Channels (infra + adapters + e2e)
echo   5. Agent + Recipe Pipeline
echo   6. Tools + AI (VLM, embeddings, TTS, vision)
echo   7. Quick smoke (P2P security only - fastest)
echo   8. Custom pytest pattern
echo   9. Distro + Security Hardening (469 tests)
echo.

set /p choice="Enter choice (1-9): "

if "%choice%"=="1" (
    echo.
    echo Running FULL regression suite [2263 tests]...
    echo ========================================
    echo Output saved to regression_results.txt
    echo.
    REM  -s disables fd-capture (pytest's tmpfile gets closed by imports,
    REM     causing "ValueError: I/O operation on closed file" abort)
    "%PYTHON_EXE%" -m pytest ^
        tests/ ^
        integrations/channels/tests/ ^
        --override-ini="addopts=" ^
        --tb=line --color=no -q -s ^
        > regression_results.txt 2>&1
    echo.
    echo ---- SUMMARY ----
    findstr /C:"passed" /C:"failed" /C:"error" regression_results.txt
    echo.
    echo Full output: regression_results.txt
) else if "%choice%"=="2" (
    echo.
    echo Running P2P Network + Security [~722 tests]...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %P2P_SECURITY_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="3" (
    echo.
    echo Running Social Platform [~148 tests]...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %SOCIAL_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="4" (
    echo.
    echo Running All Channels [~490 tests]...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %CHANNEL_INFRA_TESTS% ^
        %CHANNEL_ADAPTER_TESTS% ^
        %CHANNEL_E2E_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="5" (
    echo.
    echo Running Agent + Recipe Pipeline...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %AGENT_RECIPE_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="6" (
    echo.
    echo Running Tools + AI...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %TOOLS_AI_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="7" (
    echo.
    echo Running Quick smoke [P2P security]...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %P2P_SECURITY_TESTS% ^
        --tb=short --color=yes -q --no-header
) else if "%choice%"=="8" (
    set /p pattern="Enter pytest pattern (e.g. tests/test_file.py -k test_name): "
    echo Running custom pattern...
    "%PYTHON_EXE%" -m pytest %pattern% --tb=short --color=yes
) else if "%choice%"=="9" (
    echo.
    echo Running Distro + Security Hardening [~469 tests]...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %DISTRO_TESTS% ^
        --tb=short --color=yes -q
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
