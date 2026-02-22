@echo off
REM ============================================================
REM Cloud Mode (Central Server) Startup
REM ============================================================
REM Non-compute-heavy central server mode.
REM Intelligence comes from GPT, Claude, and other cloud LLMs.
REM No local llama.cpp needed. Standalone without Nunba app.
REM HevolveAI pip-installed for world model bridge.
REM
REM Usage:
REM   start_cloud.bat                          (uses OPENAI_API_KEY from .env)
REM   start_cloud.bat --api-key sk-xxxx        (explicit API key)
REM   start_cloud.bat --model gpt-4.1          (use full gpt-4.1 instead of mini)
REM   start_cloud.bat --endpoint https://your-azure.openai.azure.com/v1
REM ============================================================

echo ========================================
echo  Cloud Mode (Central Server)
echo  GPT / Claude / In-House Models
echo ========================================
echo.

REM Parse arguments
set CLOUD_ENDPOINT=https://api.openai.com/v1
set CLOUD_MODEL=gpt-4.1-mini
set CLOUD_KEY=

:parse_args
if "%~1"=="--endpoint" (
    set CLOUD_ENDPOINT=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--model" (
    set CLOUD_MODEL=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--api-key" (
    set CLOUD_KEY=%~2
    shift
    shift
    goto :parse_args
)
if "%~1" NEQ "" (
    shift
    goto :parse_args
)

REM API key: explicit arg > env > .env file
if "%CLOUD_KEY%"=="" (
    if defined OPENAI_API_KEY (
        set CLOUD_KEY=%OPENAI_API_KEY%
    )
)
if "%CLOUD_KEY%"=="" (
    REM Try loading from .env
    if exist "%~dp0..\.env" (
        for /f "tokens=1,2 delims==" %%a in (%~dp0..\.env) do (
            if "%%a"=="OPENAI_API_KEY" set CLOUD_KEY=%%b
        )
    )
)
if "%CLOUD_KEY%"=="" (
    echo WARNING: No API key found. Set OPENAI_API_KEY in .env or use --api-key
    echo.
)

set HEVOLVE_NODE_TIER=central
set HEVOLVE_LLM_ENDPOINT_URL=%CLOUD_ENDPOINT%
set HEVOLVE_LLM_MODEL_NAME=%CLOUD_MODEL%
set HEVOLVE_LLM_API_KEY=%CLOUD_KEY%
set HEVOLVE_AGENT_ENGINE_ENABLED=true
set ENABLE_FEDERATION=true

echo [MODE]       HEVOLVE_NODE_TIER=central
echo [LLM]        Endpoint: %CLOUD_ENDPOINT%
echo [LLM]        Model: %CLOUD_MODEL%
echo [ENGINE]     Agent engine enabled
echo [FEDERATION] Federation enabled
echo.
echo No local llama.cpp needed - intelligence from cloud APIs.
echo.

call "%~dp0run.bat"
