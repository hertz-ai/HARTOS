@echo off
REM ============================================================
REM HART OS — Master Regression Test Runner
REM ============================================================
REM Runs ALL unit/integration tests across the codebase.
REM
REM CI Mode: set CI=true && scripts\run_regression.bat
REM   Skips interactive menu, runs ALL groups.
REM ============================================================

echo ========================================
echo  HART OS Master Regression Suite
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
set PYTHONIOENCODING=utf-8
chcp 65001 >nul 2>&1

REM ===== OUTPUT DIRECTORIES =====
set REPORT_DIR=test-reports
set JUNIT_DIR=%REPORT_DIR%\junit
set LOGS_DIR=%REPORT_DIR%\logs
if not exist "%JUNIT_DIR%" mkdir "%JUNIT_DIR%"
if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"

REM ===== DEFINE TEST GROUPS =====

REM Group 1: P2P Network, Security, Agent Engine (~960+ tests)
set P2P_SECURITY_TESTS=^
    tests/unit/test_hierarchy_system.py ^
    tests/unit/test_integrity_system.py ^
    tests/unit/test_ad_hosting_rewards.py ^
    tests/unit/test_agent_network_resilience.py ^
    tests/unit/test_master_key_system.py ^
    tests/unit/test_coding_agent.py ^
    tests/unit/test_cache_restoration.py ^
    tests/unit/test_agent_engine.py ^
    tests/unit/test_node_watchdog.py ^
    tests/unit/test_auto_discovery.py ^
    tests/unit/test_agent_dashboard.py ^
    tests/unit/test_mode_aware_inference.py ^
    tests/unit/test_system_requirements.py ^
    tests/unit/test_commercial_ip_builds.py ^
    tests/unit/test_federation_upgrade.py ^
    tests/unit/test_continual_learner_gate.py ^
    tests/unit/test_gradient_sync.py ^
    tests/unit/test_thought_experiments.py

REM Group 2: Social Platform (~148 tests)
set SOCIAL_TESTS=^
    tests/unit/test_social_regression.py ^
    tests/unit/test_social_models.py ^
    tests/unit/test_social_feed.py ^
    tests/unit/test_social_search.py ^
    tests/unit/test_social_karma.py ^
    tests/unit/test_social_api.py ^
    tests/unit/test_social_naming.py

REM Group 3: Channel Infrastructure (~120 tests)
set CHANNEL_INFRA_TESTS=^
    tests/unit/test_rate_limit.py ^
    tests/unit/test_dedupe.py ^
    tests/unit/test_debounce.py ^
    tests/unit/test_retry.py ^
    tests/unit/test_channel_security.py ^
    tests/integration/test_channel_integration.py

REM Group 4: Channel Adapters (~200 tests)
set CHANNEL_ADAPTER_TESTS=^
    tests/unit/test_discord_adapter.py ^
    tests/unit/test_telegram_adapter.py ^
    tests/unit/test_web_adapter.py ^
    tests/unit/test_google_chat_adapter.py ^
    tests/unit/test_signal_adapter.py ^
    tests/unit/test_imessage_adapter.py ^
    tests/unit/test_mattermost_adapter.py ^
    tests/unit/test_nextcloud_adapter.py

REM Group 5: Channel E2E (~172 tests)
set CHANNEL_E2E_TESTS=^
    tests/integration/test_channels_e2e_regression.py ^
    tests/integration/test_channels_admin_dashboard.py ^
    tests/integration/test_channels_gateway_protocol.py ^
    tests/integration/test_channels_metrics_collector.py

REM Group 6: Agent and Recipe Pipeline (~230+ tests)
set AGENT_RECIPE_TESTS=^
    tests/unit/test_agent_creation.py ^
    tests/unit/test_recipe_generation.py ^
    tests/unit/test_reuse_mode.py ^
    tests/unit/test_action_execution.py ^
    tests/unit/test_scheduler_creation.py ^
    tests/standalone/test_autonomous_agent_suite.py ^
    tests/standalone/test_complex_agent_comprehensive.py ^
    tests/unit/test_dynamic_agents.py ^
    tests/standalone/test_nested_tasks_direct.py ^
    tests/integration/test_complete_integration.py ^
    tests/e2e/test_complete_e2e_integration.py ^
    tests/e2e/test_e2e_pipelines.py ^
    tests/unit/test_recipe_experience_healing.py

REM Group 7: Session and Messaging (~90 tests)
set SESSION_TESTS=^
    tests/unit/test_session_manager.py ^
    tests/unit/test_message_queue.py ^
    tests/unit/test_response_streaming.py ^
    tests/unit/test_preferences.py ^
    tests/unit/test_builtin_commands.py

REM Group 8: Tools and AI (~320+ tests)
set TOOLS_AI_TESTS=^
    tests/unit/test_vlm_agent.py ^
    tests/unit/test_shell_execution.py ^
    tests/unit/test_file_manager.py ^
    tests/unit/test_file_tracker.py ^
    tests/unit/test_vision_sidecar.py ^
    tests/unit/test_embeddings.py ^
    tests/unit/test_memory_search.py ^
    tests/unit/test_image_gen.py ^
    tests/unit/test_tts.py ^
    tests/unit/test_runtime_tools.py ^
    tests/unit/test_qwen3vl_computer_use.py

REM Group 9: Core, Performance, and Concurrency (~150 tests)
set CORE_PERF_TESTS=^
    tests/unit/test_core_performance.py ^
    tests/unit/test_state_management.py ^
    tests/unit/test_concurrency.py

REM Group 10: Integration and Data (~50 tests)
set INTEGRATION_TESTS=^
    tests/integration/test_integration.py ^
    tests/unit/test_redis_ledger.py

REM Group 11: Distro, Security Hardening, Deployment (~469 tests)
set DISTRO_TESTS=^
    tests/unit/test_distro_configs.py ^
    tests/unit/test_distro_tools.py ^
    tests/unit/test_pxe_server.py ^
    tests/unit/test_ota_update.py ^
    tests/unit/test_network_provisioner.py ^
    tests/unit/test_security_hardening_distro.py ^
    tests/unit/test_deployment_modes.py

REM Group 12: WS Workstream Tests (metered API, compute, budget, revenue)
set WS_TESTS=^
    tests/unit/test_budget_gate.py ^
    tests/unit/test_boot_hardening.py ^
    tests/unit/test_revenue_pipeline.py ^
    tests/unit/test_compute_config.py ^
    tests/unit/test_model_routing.py ^
    tests/unit/test_metered_recovery.py ^
    tests/unit/test_settings_api.py ^
    tests/unit/test_ad_hosting_rewards.py

REM Group 13: Security Hardening + Build Verification
set SECURITY_HARDENING_TESTS=^
    tests/unit/test_integrity_system.py ^
    tests/unit/test_federation_upgrade.py ^
    tests/unit/test_build_verification.py ^
    tests/unit/test_immutable_audit_log.py ^
    tests/unit/test_tool_allowlist.py ^
    tests/unit/test_goal_rate_limit.py ^
    tests/unit/test_action_classifier.py ^
    tests/unit/test_dlp_engine.py

REM Group 14: Resonance Tuning + Agent Personality (~100 tests)
set RESONANCE_TESTS=^
    tests/unit/test_resonance_profile.py ^
    tests/unit/test_resonance_tuner.py ^
    tests/unit/test_resonance_learning.py ^
    tests/unit/test_resonance_integration.py ^
    tests/unit/test_resonance_identifier.py ^
    tests/unit/test_biometric_signatures.py ^
    tests/unit/test_agent_personality.py

REM Group 15: E2E Realworld Resonance (12 scenarios)
set REALWORLD_TESTS=^
    tests/realworld_resonance_test.py

REM ===== CI MODE =====
if "%CI%"=="true" goto :ci_mode
if "%CI%"=="1" goto :ci_mode
if "%1"=="--ci" goto :ci_mode
goto :interactive_mode

:ci_mode
echo CI MODE: Running ALL test groups
echo ========================================
echo.

echo --- WS Workstream Tests ---
"%PYTHON_EXE%" -m pytest %WS_TESTS% --noconftest --tb=short --color=no -q --junitxml="%JUNIT_DIR%\ws_workstream.xml"
echo.

echo --- Security Hardening Tests ---
"%PYTHON_EXE%" -m pytest %SECURITY_HARDENING_TESTS% --noconftest --tb=short --color=no -q --junitxml="%JUNIT_DIR%\security_hardening.xml"
echo.

echo --- Core + Performance ---
"%PYTHON_EXE%" -m pytest %CORE_PERF_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\core_perf.xml"
echo.

echo --- Social ---
"%PYTHON_EXE%" -m pytest %SOCIAL_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\social.xml"
echo.

echo --- P2P Security ---
"%PYTHON_EXE%" -m pytest %P2P_SECURITY_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\p2p_security.xml"
echo.

echo --- Channel Infrastructure ---
"%PYTHON_EXE%" -m pytest %CHANNEL_INFRA_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\channel_infra.xml"
echo.

echo --- Channel Adapters ---
"%PYTHON_EXE%" -m pytest %CHANNEL_ADAPTER_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\channel_adapters.xml"
echo.

echo --- Channel E2E ---
"%PYTHON_EXE%" -m pytest %CHANNEL_E2E_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\channel_e2e.xml"
echo.

echo --- Agent + Recipe ---
"%PYTHON_EXE%" -m pytest %AGENT_RECIPE_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\agent_recipe.xml"
echo.

echo --- Session ---
"%PYTHON_EXE%" -m pytest %SESSION_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\session.xml"
echo.

echo --- Tools + AI ---
"%PYTHON_EXE%" -m pytest %TOOLS_AI_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\tools_ai.xml"
echo.

echo --- Integration ---
"%PYTHON_EXE%" -m pytest %INTEGRATION_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\integration.xml"
echo.

echo --- Distro ---
"%PYTHON_EXE%" -m pytest %DISTRO_TESTS% --tb=short --color=no -q --junitxml="%JUNIT_DIR%\distro.xml"
echo.

echo --- Resonance Tuning + Personality ---
"%PYTHON_EXE%" -m pytest %RESONANCE_TESTS% --noconftest --tb=short --color=no -q --junitxml="%JUNIT_DIR%\resonance.xml"
echo.

echo --- E2E Realworld Resonance ---
"%PYTHON_EXE%" -m pytest %REALWORLD_TESTS% --noconftest --tb=short --color=no -q --junitxml="%JUNIT_DIR%\realworld.xml"
echo.

echo ========================================
echo  CI Regression complete — generating consolidated report...
echo ========================================
echo.
"%PYTHON_EXE%" scripts\generate_regression_report.py --junit-dir "%JUNIT_DIR%" --output "%REPORT_DIR%\consolidated_report.txt"
echo.
echo  JUnit XML: %JUNIT_DIR%\
echo  Report:    %REPORT_DIR%\consolidated_report.txt
echo ========================================
goto :eof

:interactive_mode
echo Select regression scope:
echo.
echo   1. FULL regression (all test groups)
echo   2. P2P Network + Security
echo   3. Social Platform
echo   4. All Channels (infra + adapters + e2e)
echo   5. Agent + Recipe Pipeline
echo   6. Tools + AI
echo   7. Quick smoke (P2P security only - fastest)
echo   8. Custom pytest pattern
echo   9. Distro + Security Hardening
echo  10. WS Workstream Tests (metered API, compute, revenue)
echo  11. Security Hardening Tests (audit, DLP, classifier, etc.)
echo  12. Resonance Tuning + Agent Personality
echo  13. E2E Realworld Resonance Scenarios
echo.

set /p choice="Enter choice (1-13): "

if "%choice%"=="1" (
    echo.
    echo Running FULL regression suite...
    echo ========================================
    echo Output saved to %LOGS_DIR%\regression_full.txt
    echo.
    "%PYTHON_EXE%" -m pytest ^
        tests/unit/ ^
        tests/integration/ ^
        --override-ini="addopts=" ^
        --tb=line --color=no -q -s ^
        > "%LOGS_DIR%\regression_full.txt" 2>&1
    echo.
    echo ---- SUMMARY ----
    findstr /C:"passed" /C:"failed" /C:"error" "%LOGS_DIR%\regression_full.txt"
    echo.
    echo Full output: %LOGS_DIR%\regression_full.txt
) else if "%choice%"=="2" (
    echo.
    echo Running P2P Network + Security...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %P2P_SECURITY_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="3" (
    echo.
    echo Running Social Platform...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %SOCIAL_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="4" (
    echo.
    echo Running All Channels...
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
    set /p pattern="Enter pytest pattern: "
    echo Running custom pattern...
    "%PYTHON_EXE%" -m pytest %pattern% --tb=short --color=yes
) else if "%choice%"=="9" (
    echo.
    echo Running Distro + Security Hardening...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %DISTRO_TESTS% ^
        --tb=short --color=yes -q
) else if "%choice%"=="10" (
    echo.
    echo Running WS Workstream Tests...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %WS_TESTS% ^
        --noconftest --tb=short --color=yes -q
) else if "%choice%"=="11" (
    echo.
    echo Running Security Hardening Tests...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %SECURITY_HARDENING_TESTS% ^
        --noconftest --tb=short --color=yes -q
) else if "%choice%"=="12" (
    echo.
    echo Running Resonance Tuning + Agent Personality...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %RESONANCE_TESTS% ^
        --noconftest --tb=short --color=yes -q
) else if "%choice%"=="13" (
    echo.
    echo Running E2E Realworld Resonance Scenarios...
    echo ========================================
    "%PYTHON_EXE%" -m pytest ^
        %REALWORLD_TESTS% ^
        --noconftest --tb=short --color=yes -q
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
