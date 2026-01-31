@echo off
REM ============================================================
REM HevolveBot LangChain Agent - Interactive Test Runner
REM ============================================================
REM Runs unit and integration tests with selectable suites.
REM ============================================================

echo ========================================
echo  Running Test Suite
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
echo Select test suite:
echo   1. All tests (comprehensive)
echo   2. HevolveBot channel regression tests (91 tests)
echo   3. Master test suite
echo   4. Autonomous agent suite
echo   5. Dynamic agents tests
echo   6. Complex agent comprehensive tests
echo   7. Quick smoke test (channels only)
echo   8. Custom pytest pattern
echo.

set /p choice="Enter choice (1-8): "

if "%choice%"=="1" (
    echo Running all tests...
    "%PYTHON_EXE%" -m pytest tests/ integrations/channels/tests/ -v --tb=short --color=yes
) else if "%choice%"=="2" (
    echo Running HevolveBot channel regression tests...
    "%PYTHON_EXE%" -m pytest integrations/channels/tests/test_e2e_regression.py -v --tb=short --color=yes
) else if "%choice%"=="3" (
    echo Running master test suite...
    "%PYTHON_EXE%" test_master_suite.py
) else if "%choice%"=="4" (
    echo Running autonomous agent suite...
    "%PYTHON_EXE%" test_autonomous_agent_suite.py
) else if "%choice%"=="5" (
    echo Running dynamic agents tests...
    "%PYTHON_EXE%" -m pytest test_dynamic_agents.py -v --tb=short
) else if "%choice%"=="6" (
    echo Running complex agent comprehensive tests...
    "%PYTHON_EXE%" -m pytest test_complex_agent_comprehensive.py -v --tb=short
) else if "%choice%"=="7" (
    echo Running quick smoke test...
    "%PYTHON_EXE%" -m pytest integrations/channels/tests/test_e2e_regression.py -k "TestModuleImports" -v --tb=short
) else if "%choice%"=="8" (
    set /p pattern="Enter pytest pattern (e.g. tests/test_file.py -k test_name): "
    echo Running custom pattern...
    "%PYTHON_EXE%" -m pytest %pattern% -v --tb=short --color=yes
) else (
    echo Invalid choice
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Test run complete
echo ========================================
pause
