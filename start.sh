#!/bin/bash
set -e

# Start FastAPI backend (log stderr to stdout so Cloud Run captures it)
echo "Starting FastAPI backend..."
cd /app && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 2>&1 &
BACKEND_PID=$!

# Give backend a moment to start (or fail)
sleep 3

# Check if backend is still running
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo "ERROR: FastAPI backend failed to start!"
    wait $BACKEND_PID
    exit 1
fi

echo "Backend started (PID $BACKEND_PID)"

# Start Next.js frontend
echo "Starting Next.js frontend..."
cd /app/frontend && npx next start --port 8080 --hostname 0.0.0.0 2>&1 &
FRONTEND_PID=$!

# Wait for any process to exit
wait -n
exit $?
