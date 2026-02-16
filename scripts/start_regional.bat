@echo off
REM ============================================================
REM Regional Mode Startup
REM ============================================================
REM Networked mode with a regional LLM host.
REM Connects to a regional llama.cpp or vLLM server and
REM participates in the regional gossip network.
REM
REM Usage:
REM   start_regional.bat --host http://regional-server:8080/v1
REM   start_regional.bat --host http://10.0.1.5:8080/v1 --model Qwen3-VL-4B-Instruct
REM   start_regional.bat  (reads HEVOLVE_LLM_ENDPOINT_URL from .env)
REM ============================================================

echo ========================================
echo  Regional Mode (Networked LLM Host)
echo ========================================
echo.

REM Parse arguments
set LLM_HOST=
set LLM_MODEL=Qwen3-VL-4B-Instruct
set LLM_KEY=dummy

:parse_args
if "%~1"=="--host" (
    set LLM_HOST=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--model" (
    set LLM_MODEL=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--api-key" (
    set LLM_KEY=%~2
    shift
    shift
    goto :parse_args
)
if "%~1" NEQ "" (
    shift
    goto :parse_args
)

REM Validate host
if "%LLM_HOST%"=="" (
    if defined HEVOLVE_LLM_ENDPOINT_URL (
        set LLM_HOST=%HEVOLVE_LLM_ENDPOINT_URL%
    ) else (
        echo ERROR: Regional host URL required.
        echo.
        echo Usage:
        echo   start_regional.bat --host http://regional-server:8080/v1
        echo.
        echo Or set HEVOLVE_LLM_ENDPOINT_URL in your .env file.
        pause
        exit /b 1
    )
)

set HEVOLVE_NODE_TIER=regional
set HEVOLVE_LLM_ENDPOINT_URL=%LLM_HOST%
set HEVOLVE_LLM_MODEL_NAME=%LLM_MODEL%
set HEVOLVE_LLM_API_KEY=%LLM_KEY%
set HEVOLVE_AGENT_ENGINE_ENABLED=true

echo [MODE]   HEVOLVE_NODE_TIER=regional
echo [LLM]    Endpoint: %LLM_HOST%
echo [LLM]    Model: %LLM_MODEL%
echo [ENGINE] Agent engine enabled
echo.

call "%~dp0run.bat"
