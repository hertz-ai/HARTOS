@echo off
REM ============================================================
REM Flat Mode (Local) Startup
REM ============================================================
REM Desktop/development mode with local llama.cpp inference.
REM Llama.cpp should be running on localhost (default port 8080)
REM or started by Nunba desktop app.
REM
REM Usage:
REM   start_flat.bat                   (default port 8080)
REM   start_flat.bat --llm-port 8081   (custom llama.cpp port)
REM ============================================================

echo ========================================
echo  Flat Mode (Local llama.cpp)
echo ========================================
echo.

REM Parse optional --llm-port argument
set LLM_PORT=8080
:parse_args
if "%~1"=="--llm-port" (
    set LLM_PORT=%~2
    shift
    shift
    goto :parse_args
)

set HEVOLVE_NODE_TIER=flat
set LLAMA_CPP_PORT=%LLM_PORT%

echo [MODE] HEVOLVE_NODE_TIER=flat
echo [LLM]  llama.cpp on localhost:%LLM_PORT%
echo.

call "%~dp0run.bat"
