#!/bin/bash
# HevolveBot Admin Dashboard - Unix/Linux/macOS Startup Script

echo "========================================"
echo "  HevolveBot Admin Dashboard"
echo "========================================"
echo

# Change to script directory
cd "$(dirname "$0")"

# Check if node_modules exists
if [ ! -d "node_modules" ]; then
    echo "Installing dependencies..."
    echo
    npm install
    if [ $? -ne 0 ]; then
        echo
        echo "ERROR: Failed to install dependencies."
        echo "Make sure Node.js and npm are installed."
        exit 1
    fi
    echo
fi

echo "Starting development server..."
echo
echo "Dashboard will be available at: http://localhost:3000"
echo "Press Ctrl+C to stop the server."
echo

npm run dev
