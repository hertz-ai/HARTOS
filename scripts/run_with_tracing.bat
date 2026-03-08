@echo off
REM ============================================================
REM HART OS - Server WITH Socket Tracing
REM ============================================================
REM This enables real-time PyCharm socket tracing for debugging.
REM Trace events are sent to localhost:5678 for the TrueFlow plugin.
REM ============================================================

echo ========================================
echo  Starting HART OS
echo  WITH SOCKET TRACING
echo ========================================
echo.

cd /d %~dp0\..

REM ===== PYTHON ENVIRONMENT =====
if exist "venv310\Scripts\python.exe" (
    set PYTHON_EXE=%~dp0..\venv310\Scripts\python.exe
    echo [ENV] Using venv310
) else if exist "C:\Python310\python.exe" (
    set PYTHON_EXE=C:\Python310\python.exe
    echo [ENV] Using system Python 3.10
) else (
    echo ERROR: Python 3.10 not found.
    echo Please create venv310 or install Python 3.10
    pause
    exit /b 1
)

REM ===== ENABLE PYCHARM SOCKET TRACING =====
REM NOTE: Using .pycharm_plugin (dot prefix) - this is where Auto-Integrate puts the files
set PYTHONPATH=%~dp0..\.pycharm_plugin\runtime_injector;%PYTHONPATH%
set PYCHARM_PLUGIN_TRACE_ENABLED=1
set PYCHARM_PLUGIN_SOCKET_TRACE=1
set PYCHARM_PLUGIN_TRACE_PORT=5678
set PYCHARM_PLUGIN_TRACE_HOST=127.0.0.1

REM Create trace output directory
set TRACE_DIR=%~dp0..\traces
if not exist "%TRACE_DIR%" mkdir "%TRACE_DIR%"

echo [TRACING] Socket trace server will start on localhost:5678
echo [TRACING] Connect from PyCharm: Attach to Server button
echo [TRACING] PYTHONPATH=%PYTHONPATH%
echo [TRACING] Trace Dir: %TRACE_DIR%
echo.

REM ===== ENVIRONMENT VARIABLES =====
if exist ".env" (
    echo [ENV] Loading .env file...
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set "%%a=%%b"
    )
)

REM WAMP/Crossbar connection
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

REM ===== DEPENDENCY CHECK =====
echo [CHECK] Verifying critical dependencies...
"%PYTHON_EXE%" -c "import langchain; import autogen; import chromadb; print('  langchain:', langchain.__version__); print('  autogen:', autogen.__version__); print('  chromadb:', chromadb.__version__)" 2>nul
if errorlevel 1 (
    echo WARNING: Some dependencies missing.
    echo.
)
echo.

REM ===== START SERVER =====
echo ========================================
echo  Server Configuration
echo ========================================
echo  API:        http://localhost:6777
echo  Health:     http://localhost:6777/status
echo  Tracing:    localhost:5678 (PyCharm TrueFlow)
echo  Chat:       POST http://localhost:6777/chat
echo  Time Agent: POST http://localhost:6777/time_agent
echo  VLM Agent:  POST http://localhost:6777/visual_agent
echo ========================================
echo.
echo Starting server with tracing...
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" langchain_gpt_api.py

if errorlevel 1 (
    echo.
    echo ERROR: Server failed to start
    echo Check the error messages above
    pause
)
