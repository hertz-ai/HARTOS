@echo off
REM ============================================================
REM HevolveBot LangChain Agent - End-to-End Test Runner
REM ============================================================
REM Runs comprehensive E2E tests. Starts server if not running.
REM Supports Docker-based and local test modes.
REM ============================================================

echo ========================================
echo  End-to-End Test Suite
echo ========================================
echo.
echo This will:
echo   1. Start API server (if not running)
echo   2. Run comprehensive E2E tests
echo   3. Test complete agent workflows
echo   4. Validate create/reuse recipe pipeline
echo.
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

echo.
echo Checking if API server is running...
curl -s http://localhost:6777/status > nul 2>&1

if errorlevel 1 (
    echo.
    echo API server not running. Starting now...
    echo This may take a moment for model loading...
    echo.

    start "HevolveBot API Server" "%PYTHON_EXE%" langchain_gpt_api.py

    REM Wait for server to be ready
    echo Waiting for server to start...
    timeout /t 15 /nobreak > nul

    REM Check again
    curl -s http://localhost:6777/status > nul 2>&1
    if errorlevel 1 (
        echo.
        echo WARNING: Server may not be ready yet
        echo Waiting 15 more seconds...
        timeout /t 15 /nobreak > nul
    )
    echo.
) else (
    echo API server is already running!
    echo.
)

echo ========================================
echo  Select Test Mode
echo ========================================
echo.
echo   1. Channel regression tests (91 tests - quick)
echo   2. Master test suite (comprehensive)
echo   3. Autonomous agent suite (agent workflows)
echo   4. Docker E2E tests (requires Docker)
echo   5. All local tests (everything)
echo   6. Coverage report (with HTML output)
echo.

set /p choice="Enter choice (1-6): "

if "%choice%"=="1" (
    echo Running channel regression tests...
    "%PYTHON_EXE%" -m pytest tests/integration/test_channels_e2e_regression.py -v -s --tb=short
) else if "%choice%"=="2" (
    echo Running master test suite...
    "%PYTHON_EXE%" tests/standalone/test_master_suite.py
) else if "%choice%"=="3" (
    echo Running autonomous agent suite...
    "%PYTHON_EXE%" tests/standalone/test_autonomous_agent_suite.py
) else if "%choice%"=="4" (
    echo Running Docker E2E tests...
    echo Checking Docker availability...
    docker --version > nul 2>&1
    if errorlevel 1 (
        echo ERROR: Docker not found. Install Docker Desktop or add to PATH.
        pause
        exit /b 1
    )
    echo Building and running Docker test environment...
    docker-compose -f docker-compose.test.yml up --build --abort-on-container-exit
) else if "%choice%"=="5" (
    echo Running ALL local tests...
    "%PYTHON_EXE%" -m pytest tests/unit/ tests/integration/ tests/e2e/ -v --tb=short --color=yes
) else if "%choice%"=="6" (
    echo Running tests with coverage...
    if exist htmlcov rmdir /s /q htmlcov
    "%PYTHON_EXE%" -m pytest tests/integration/test_channels_e2e_regression.py -v --tb=short ^
        --cov=integrations/channels ^
        --cov-report=html:htmlcov ^
        --cov-report=term-missing
    if exist htmlcov\index.html (
        echo.
        echo [SUCCESS] Coverage report generated at htmlcov\index.html
        echo Opening in browser...
        start htmlcov\index.html
    )
) else (
    echo Invalid choice
    pause
    exit /b 1
)

echo.
echo ========================================
echo  E2E Test Complete
echo ========================================
echo.

REM Ask if user wants to stop server
set /p stop_server="Stop API server? (y/n): "

if /i "%stop_server%"=="y" (
    echo Stopping API server...
    taskkill /FI "WindowTitle eq HevolveBot API Server*" /F > nul 2>&1
    echo Server stopped.
)

pause
