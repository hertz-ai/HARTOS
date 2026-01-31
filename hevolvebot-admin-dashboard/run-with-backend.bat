@echo off
REM HevolveBot Admin Dashboard + Backend - Windows Startup Script

echo ========================================
echo   HevolveBot Admin Dashboard + Backend
echo ========================================
echo.

cd /d "%~dp0"

REM Check if node_modules exists
if not exist "node_modules" (
    echo Installing dashboard dependencies...
    echo.
    call npm install
    if errorlevel 1 (
        echo ERROR: Failed to install dashboard dependencies.
        pause
        exit /b 1
    )
    echo.
)

echo Starting Flask backend in background...
cd ..
start "Flask Backend" cmd /c "python langchain_gpt_api.py"

REM Wait a moment for backend to start
timeout /t 3 /nobreak >nul

echo Starting dashboard...
cd hevolvebot-admin-dashboard

echo.
echo Backend API: http://localhost:5000
echo Dashboard:   http://localhost:3000
echo.
echo Press Ctrl+C to stop the dashboard.
echo Close the "Flask Backend" window to stop the backend.
echo.

call npm run dev

pause
