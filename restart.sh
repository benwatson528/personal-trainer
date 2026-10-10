#!/usr/bin/env bash
# Restart the Personal Trainer backend and Open WebUI.

set -euo pipefail

cd "$(dirname "$0")"

echo "==> Stopping PT backend..."
kill $(lsof -t -i:8000) 2>/dev/null || true
sleep 1

echo "==> Restarting Open WebUI..."
docker restart open-webui 2>/dev/null || echo "   (Open WebUI container not found — start it manually)"

echo "==> Starting PT backend..."
PYTHONPATH=src .venv/bin/uvicorn "app.api:create_app" --factory --port 8000 &
SERVER_PID=$!

echo "==> Waiting for PT backend to be ready..."
for i in $(seq 1 30); do
  if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo "==> PT backend is ready at http://localhost:8000"
    echo "==> Open WebUI is at http://localhost:3000"
    exit 0
  fi
  sleep 1
done

echo "ERROR: PT backend did not become ready in time." >&2
exit 1
