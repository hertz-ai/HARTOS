@echo off
REM HevolveBot Admin Dashboard - Windows Startup Script

echo ========================================
echo   HevolveBot Admin Dashboard
echo ========================================
echo.

cd /d "%~dp0"

REM Check if node_modules exists
if not exist "node_modules" (
    echo Installing dependencies...
    echo.
    call npm install
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Make sure Node.js and npm are installed.
        pause
        exit /b 1
    )
    echo.
)

echo Starting development server...
echo.
echo Dashboard will be available at: http://localhost:3000
echo Press Ctrl+C to stop the server.
echo.

call npm run dev

pause
