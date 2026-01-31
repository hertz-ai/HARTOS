#!/bin/bash
# HevolveBot Admin Dashboard + Backend - Unix/Linux/macOS Startup Script

echo "========================================"
echo "  HevolveBot Admin Dashboard + Backend"
echo "========================================"
echo

# Change to script directory
cd "$(dirname "$0")"

# Check if node_modules exists
if [ ! -d "node_modules" ]; then
    echo "Installing dashboard dependencies..."
    echo
    npm install
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to install dashboard dependencies."
        exit 1
    fi
    echo
fi

# Start Flask backend in background
echo "Starting Flask backend in background..."
cd ..
python langchain_gpt_api.py &
BACKEND_PID=$!

# Wait a moment for backend to start
sleep 3

echo "Starting dashboard..."
cd hevolvebot-admin-dashboard

echo
echo "Backend API: http://localhost:5000 (PID: $BACKEND_PID)"
echo "Dashboard:   http://localhost:3000"
echo
echo "Press Ctrl+C to stop both services."
echo

# Trap Ctrl+C to kill backend
trap "echo 'Stopping services...'; kill $BACKEND_PID 2>/dev/null; exit 0" INT TERM

npm run dev

# Cleanup
kill $BACKEND_PID 2>/dev/null
