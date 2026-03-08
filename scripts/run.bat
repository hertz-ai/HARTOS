@echo off
REM ============================================================
REM HART OS - Server Startup Script
REM ============================================================
REM This is the RECOMMENDED way to start the server.
REM Starts the Flask API server on port 6777 using Waitress.
REM ============================================================

echo ========================================
echo  Starting HART OS
echo  Flask API Server (port 6777)
echo ========================================
echo.

cd /d %~dp0\..

REM ===== PYTHON ENVIRONMENT =====
REM Prefer venv310 (Python 3.10 required for pydantic 1.10.9)
if exist "venv310\Scripts\python.exe" (
    set PYTHON_EXE=%~dp0..\venv310\Scripts\python.exe
    echo [ENV] Using venv310: %~dp0..\venv310\Scripts\python.exe
) else if exist "C:\Python310\python.exe" (
    set PYTHON_EXE=C:\Python310\python.exe
    echo [ENV] Using system Python 3.10: C:\Python310\python.exe
) else (
    echo ERROR: Python 3.10 not found.
    echo Please create venv310 or install Python 3.10
    echo   python3.10 -m venv venv310
    echo   venv310\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

REM Verify Python version
"%PYTHON_EXE%" -c "import sys; assert sys.version_info[:2] == (3,10), f'Need Python 3.10, got {sys.version}'" 2>nul
if errorlevel 1 (
    echo WARNING: Python version may not be 3.10 - pydantic compatibility not guaranteed
    echo.
)

REM ===== ENVIRONMENT VARIABLES =====
REM Load .env if exists
if exist ".env" (
    echo [ENV] Loading .env file...
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set "%%a=%%b"
    )
)

REM WAMP/Crossbar connection (for real-time pub/sub)
if not defined WAMP_URL (
    set WAMP_URL=ws://azurekong.hertzai.com:8088/ws
)
if not defined WAMP_REALM (
    set WAMP_REALM=realm1
)
echo [WAMP] URL: %WAMP_URL%
echo [WAMP] Realm: %WAMP_REALM%
echo.

REM UTF-8 support
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=UTF-8
set PYTHONUNBUFFERED=1

REM ===== AGENT LIGHTNING TRACING =====
set AGENT_LIGHTNING_ENABLED=true
set AGENT_LIGHTNING_TRACE_DIR=%~dp0..\agent_data\lightning_traces
if not exist "%AGENT_LIGHTNING_TRACE_DIR%" mkdir "%AGENT_LIGHTNING_TRACE_DIR%"
echo [LIGHTNING] Trace dir: %AGENT_LIGHTNING_TRACE_DIR%

REM ===== SIMPLEMEM LONG-TERM MEMORY =====
set SIMPLEMEM_ENABLED=true
if not defined SIMPLEMEM_API_KEY (
    echo [SIMPLEMEM] SIMPLEMEM_API_KEY not set - will use local DB mode
) else (
    echo [SIMPLEMEM] Long-term memory enabled with API key
)
echo.

REM ===== DEPENDENCY CHECK =====
echo [CHECK] Verifying critical dependencies...
"%PYTHON_EXE%" -c "import langchain; import autogen; import chromadb; print('  langchain:', langchain.__version__); print('  autogen:', autogen.__version__); print('  chromadb:', chromadb.__version__)" 2>nul
if errorlevel 1 (
    echo.
    echo WARNING: Some dependencies missing. Run:
    echo   "%PYTHON_EXE%" -m pip install -r requirements.txt
    echo.
)
echo.

REM ===== START SERVER =====
echo ========================================
echo  Server Configuration
echo ========================================
echo  API:        http://localhost:6777
echo  Health:     http://localhost:6777/status
echo  Chat:       POST http://localhost:6777/chat
echo  Time Agent: POST http://localhost:6777/time_agent
echo  VLM Agent:  POST http://localhost:6777/visual_agent
echo ========================================
echo.
echo Starting server...
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" langchain_gpt_api.py

if errorlevel 1 (
    echo.
    echo ERROR: Server failed to start
    echo Check the error messages above
    pause
)
